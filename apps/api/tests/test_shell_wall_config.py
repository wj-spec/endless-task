"""切片 10 T1（Step 1/2）：`run_shell` 的两条**工具层墙**可配置，默认逐字不变。

为什么单独开一个文件：这两条墙与 run 预算（`test_run_budget.py` /
`test_app_settings.py` 的三档）是**两件事**——一次"需要跑 10 分钟构建"的长活，
即使 run 预算给到 900/3600 秒，也会在工具层被反复掐死：

- **外层墙**（`definition.timeout_seconds`）：`execution_impl/coordinator.py` 读它
  做 `asyncio.wait(timeout=...)`。它过去被**类级定义**钉死成 120 秒，于是
  `ENDLESS_TASK_SHELL_TIMEOUT_SECONDS=600` 只改了内层 runner，外层仍在 120 秒判
  `tool_timeout`——设置看起来"调不动"。
- **无输出看门狗**（`shell_runner.py`）：多久没有 stdout/stderr 就认为卡死。
  与总时长墙是两件事（"安静多久算死" vs "最长允许跑多久"）。

验收对应：① 外层墙跟随设置（且默认仍是 120）、② 看门狗可配 + 下限 + 0 = 关闭、
⑤ 默认形态下 `run_shell` 的定义逐字段与类级定义一致。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.api.app_settings import AppSettings
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    StaticToolApprovalGate,
    ToolApprovalDecision,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolRegistry
from endless_task.workspace_runtime import shell_tool as shell_tool_module
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.shell_runner import (
    DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
    DEFAULT_SHELL_TIMEOUT_SECONDS,
    MIN_NO_CHANGE_TIMEOUT_SECONDS,
    ShellResult,
    clamp_shell_timeout_seconds,
    resolve_no_change_timeout_seconds,
    run_shell_command,
)
from endless_task.workspace_runtime.shell_tool import RunShellTool
# 跨测试模块复用 ScriptedProvider（仓库既有先例：test_app_settings 从
# test_provider_configuration 取 isolated_cwd）。
from tests.test_runtime_v2_execution import ScriptedProvider


def _call(conversation_id: str = "conv_1", **arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="run_shell",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-13T00:00:00.000Z",
    )


class _FakeClock:
    """每次读表前进 ``step`` 秒的假时钟（"可参数化的时间源"）。

    为什么必须能注入时钟：看门狗与总时长墙都是"秒级"语义，用真时钟测就只能真等
    60/120 秒。假时钟让"阈值 X 秒后才杀"变成一个要么红要么绿的确定性断言。
    """

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


class RunShellDefinitionTest(unittest.TestCase):
    """① 外层墙跟随设置；⑤ 默认形态的定义逐字段不变。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.effect_log = EffectLog(Path(self._temp.name) / "logs")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _tool(self, **kwargs) -> RunShellTool:
        # resolver 只是构造参数：这一组用例只看定义，不执行命令。
        return RunShellTool(object(), self.effect_log, **kwargs)

    def test_default_instance_definition_is_field_for_field_identical(self) -> None:
        """⑤：不设任何东西时，实例定义与类级定义（= 今天的面）**逐字段相等**。

        这是"默认形态零改动"的直接证据：任何字段（描述/schema/超时/输出上限）
        漂移都会让这条断言红。
        """
        tool = self._tool()
        self.assertEqual(RunShellTool.definition, tool.definition)
        self.assertEqual(DEFAULT_SHELL_TIMEOUT_SECONDS, tool.definition.timeout_seconds)
        self.assertEqual(120.0, tool.definition.timeout_seconds)

    def test_class_level_definition_keeps_the_default_wall(self) -> None:
        """类级定义是"按类注册"路径（面快照/策略对账用例）看到的唯一来源，不要动。"""
        self.assertEqual(120.0, RunShellTool.definition.timeout_seconds)
        self.assertEqual("run_shell", RunShellTool.definition.name)
        self.assertEqual(32_000, RunShellTool.definition.max_output_characters)

    def test_configured_timeout_becomes_the_definition_timeout(self) -> None:
        """① 设成 600 → 外层墙 600（不再是类级 120）；超上限与极小值按同一把尺子钳。"""
        for configured, expected in (
            (120.0, 120.0),          # 默认
            (600.0, 600.0),          # 设置到上限
            (7200.0, 600.0),         # 超过内层上限 → 与外层同源地钳到 600
            (0.5, 1.0),              # 极小 → 下限 1 秒
            (None, None),            # 只为对齐元组长度（下面跳过）
        ):
            if configured is None:
                continue
            with self.subTest(configured=configured):
                tool = self._tool(timeout_seconds=configured)
                self.assertEqual(expected, tool.definition.timeout_seconds)
                # 内外层同源：外层定义 == 内层 runner 实际生效的墙
                self.assertEqual(
                    clamp_shell_timeout_seconds(configured),
                    tool.definition.timeout_seconds,
                )

    def test_settings_value_reaches_the_definition(self) -> None:
        """装配链：设置 → 工具定义（`wiring.py` 传的就是这个值）。"""
        settings = AppSettings(
            database_path=Path(self._temp.name) / "x.db",
            shell_timeout_seconds=600.0,
        )
        tool = self._tool(
            timeout_seconds=settings.shell_timeout_seconds,
            no_change_timeout_seconds=settings.shell_no_change_timeout_seconds,
        )
        self.assertEqual(600.0, tool.definition.timeout_seconds)

        default = AppSettings(database_path=Path(self._temp.name) / "y.db")
        default_tool = self._tool(timeout_seconds=default.shell_timeout_seconds)
        self.assertEqual(120.0, default_tool.definition.timeout_seconds)


class _BoundWorkspace:
    """真实 resolver 夹具：shell 工具需要"会话 → 已绑定工作区根"。"""

    def __init__(self, directory: Path) -> None:
        self.database = Database(directory / "shell-wall.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.root = directory / "ws"
        self.root.mkdir()
        self.workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(self.workspace.id, str(self.root))
        self.conversation = self.chat.create_or_reuse_empty_conversation(
            self.workspace.id
        )
        self.effect_log = EffectLog(directory / "logs")

    def run_for(self):
        lane = self.repository.create_lane(conversation_id=self.conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=self.conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "跑一条长命令"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=self.conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )


class ShellOuterWallEndToEndTest(unittest.TestCase):
    """① 端到端：外层墙真的等于配置值——用**注入的假 runner**，不真等 200 秒。

    为什么走真的 `AgentRunExecutor` 而不是直接调协调器的私有方法：外层超时的读取点
    是 `execution_impl/coordinator.py:940` 的
    ``timeout_seconds = tool.definition.timeout_seconds``。只有让真实的执行器跑一次
    工具调用，才能同时钉住"定义里的值"和"谁在用它"这两半。
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.workspace = _BoundWorkspace(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_configured_wall_fires_instead_of_the_old_120(self) -> None:
        # 假 runner：命令"静默地"跑 3 秒（真实等价物是 200 秒的构建/下载）。
        # 它让这个用例只花 ~1 秒，而不是 120 秒或 200 秒。
        async def _silent_runner(**kwargs) -> ShellResult:
            del kwargs
            await asyncio.sleep(3.0)
            return ShellResult(stdout="", stderr="", exit_code=0)

        # 墙配成 1 秒（= 下限）。旧行为下这里读的是类级 120 秒 → 工具会在 3 秒后
        # "成功"，断言随之变红。
        tool = RunShellTool(
            self.workspace.resolver,
            self.workspace.effect_log,
            timeout_seconds=1.0,
            no_change_timeout_seconds=60.0,
        )
        self.assertEqual(1.0, tool.definition.timeout_seconds)
        registry = ToolRegistry()
        registry.register(tool)
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="run_shell",
                        arguments={"command": "sleep 200"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=5,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("命令超时了，换个方式。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=7,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        executor = AgentRunExecutor(
            repository=self.workspace.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            # run_shell 是 REQUIRED：这里用一个恒批的闸门，把注意力留在超时上。
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )
        run = self.workspace.run_for()

        started = time.monotonic()
        with patch.object(shell_tool_module, "run_shell_command", _silent_runner):
            result = asyncio.run(
                executor.execute(run.id, cancellation_token=CancellationToken())
            )
        elapsed = time.monotonic() - started

        # 工具墙是"工具失败"，不是"运行失败"：运行照常收尾。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        # 1 秒的墙先到——远早于假 runner 自己的 3 秒，更不是旧的 120 秒。
        self.assertLess(elapsed, 2.5)
        executions = self.workspace.repository.list_tool_executions(
            result.model_turn_ids[0]
        )
        self.assertEqual(1, len(executions))
        self.assertEqual(ToolExecutionStatus.FAILED, executions[0].status)
        # 模型看到的是外层墙的 tool_timeout 文案（而不是 runner 的正常返回）。
        self.assertIn(
            "工具执行超时",
            provider.requests[1].messages[-1].content,
        )


class NoChangeWatchdogTest(unittest.IsolatedAsyncioTestCase):
    """② 无输出看门狗：可配、有下限、0 = 关闭；默认仍是 60 秒。"""

    def test_default_threshold_is_sixty_seconds(self) -> None:
        self.assertEqual(60.0, DEFAULT_NO_CHANGE_TIMEOUT_SECONDS)
        self.assertEqual(
            60.0,
            AppSettings(
                database_path=Path("/tmp/nc-default.db")
            ).shell_no_change_timeout_seconds,
        )

    def test_normalisation_semantics(self) -> None:
        # 语义表：0/负 = 关闭；(0, 下限) 钳到下限；默认与合法值原样保留。
        self.assertEqual(60.0, resolve_no_change_timeout_seconds(60.0))
        self.assertEqual(30.0, resolve_no_change_timeout_seconds(30.0))
        self.assertEqual(0.0, resolve_no_change_timeout_seconds(0))
        self.assertEqual(0.0, resolve_no_change_timeout_seconds(-5.0))
        self.assertEqual(
            MIN_NO_CHANGE_TIMEOUT_SECONDS, resolve_no_change_timeout_seconds(0.2)
        )
        self.assertEqual(1.0, MIN_NO_CHANGE_TIMEOUT_SECONDS)

    async def _watchdog_run(self, *, threshold: float, clock: _FakeClock):
        with tempfile.TemporaryDirectory() as directory:
            return await run_shell_command(
                command="sleep 30",
                cwd=Path(directory),
                # 总时长墙放到看门狗够不着的地方：本用例只问"安静多久被杀"。
                timeout_seconds=600.0,
                no_change_timeout_seconds=threshold,
                clock=clock,
            )

    async def test_watchdog_kills_after_the_configured_quiet_seconds(self) -> None:
        clock = _FakeClock(step=5.0)
        result = await self._watchdog_run(threshold=60.0, clock=clock)
        self.assertTrue(result.no_change_timeout)
        # 假时钟确实被推过了阈值：说明它等到"安静满 60 秒"才动手，不是立刻杀。
        self.assertGreaterEqual(clock.value, 60.0)

    async def test_tiny_threshold_is_clamped_to_the_floor(self) -> None:
        """0.2 秒与 1.0 秒必须给出**同一**行为——0.2 被钳到下限，不会"一有延迟就杀"。"""
        tiny = _FakeClock(step=0.1)
        tiny_result = await self._watchdog_run(threshold=0.2, clock=tiny)
        floor = _FakeClock(step=0.1)
        floor_result = await self._watchdog_run(
            threshold=MIN_NO_CHANGE_TIMEOUT_SECONDS, clock=floor
        )
        self.assertTrue(tiny_result.no_change_timeout)
        self.assertTrue(floor_result.no_change_timeout)
        self.assertEqual(floor.value, tiny.value)
        # 它没有在"第一次轮询"就杀（旧行为下 0 值会；0.2 秒的语义也不该是立刻）。
        self.assertGreater(tiny.value, 1.0)

    async def test_zero_disables_the_watchdog(self) -> None:
        """0 = 关闭看门狗（显式 opt-out）：一条安静 1 秒的命令不会被判卡死。"""
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = await run_shell_command(
                command="sleep 1",
                cwd=Path(directory),
                timeout_seconds=30.0,
                no_change_timeout_seconds=0,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(0, result.exit_code)
        self.assertFalse(result.no_change_timeout)
        self.assertFalse(result.timed_out)
        # 关闭 ≠ "立刻杀"：真的等到了命令自己跑完。
        self.assertGreaterEqual(elapsed, 0.9)


if __name__ == "__main__":
    unittest.main()
