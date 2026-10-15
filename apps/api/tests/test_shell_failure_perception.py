"""26 号 N1：失败感知回路 —— 非零退出/超时是「失败」，Agent 看得见、账本数得着。

## 这组用例守的是什么

旧的 run_shell：`error` 恒 None、执行状态恒 COMPLETED，失败只以 content 尾注
（`[退出码 N]`）表达 ⇒ eval 的 `tool_failure_count`、LoopDetection、
failure_memory **全都看不见它**，模型也只有一行尾注可依据。N1 之后：

- 非零退出 → `ToolResult.error = ToolCallError("command_failed")` + 状态 FAILED；
- 超时（含无输出看门狗）→ `command_timeout` + FAILED；
- stdout/stderr **分字段**进 `structured_content`（此前只以 `[stderr]` 混排）；
- content 顶部加 `[命令失败：…]` 横幅（尾注保留，兼容既有子串断言）；
- 协调器 error 路径：工具自带输出时「输出为主 + 失败模板尾注」，且
  `structured_content` 不再被丢掉；`ToolResult.failed()` 一族文案字节不变。

## 判别性

工具层四条（成功/失败/超时/分流字段）+ 协调器层一条真 `AgentRunExecutor`：
「工具 FAILED 但 run 照常 COMPLETED，模型既见输出又见失败归因」。
变异地刀（登记在 26 号变更流水）：error 置回 None ⇒ 工具层红；
协调器 content 改回纯模板 ⇒ 集成层「模型见输出」红。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.execution_env import FakeExecutionEnvironment, NetworkMode, ProcessResult
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    RunStatus,
    StaticToolApprovalGate,
    ToolApprovalDecision,
    ToolExecutionStatus,
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
from endless_task.workspace_runtime.shell_runner import ShellResult
from endless_task.workspace_runtime.shell_tool import RunShellTool
from tests.test_runtime_v2_execution import ScriptedProvider


def _call(conversation_id: str, command: str) -> ToolCall:
    return ToolCall(
        id="call_np",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="run_shell",
        arguments={"command": command},
        status=ToolCallStatus.CREATED,
        created_at="2026-10-13T00:00:00.000Z",
    )


def _receipt():
    from endless_task.agent_platform import EffectOutcome, EffectReceipt

    return EffectReceipt(
        effect_id="effect_np",
        tool_call_id="call_np",
        effect_type="process",
        target=".",
        started_at="2026-10-13T00:00:00Z",
        outcome=EffectOutcome.COMMITTED,
        backend="fake",
        safe_summary="fake",
        committed_at="2026-10-13T00:00:01Z",
    )


class _ToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "perception.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.root = Path(self._temp.name) / "ws"
        self.root.mkdir()
        self.workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(self.workspace.id, str(self.root))
        self.conversation = self.chat.create_or_reuse_empty_conversation(
            self.workspace.id
        )
        self.effect_log = EffectLog(Path(self._temp.name) / "logs")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _run(self, process_result: ProcessResult):
        backend = FakeExecutionEnvironment(process_result=process_result)
        tool = RunShellTool(
            self.resolver,
            self.effect_log,
            execution_backend=backend,
            sandbox_network_mode=NetworkMode.DENY,
        )
        return asyncio.run(
            tool.execute(_call(self.conversation.id, "run"), CancellationToken())
        )


class ToolLevelPerceptionTest(_ToolCase):
    def test_zero_exit_is_still_a_clean_success(self) -> None:
        """正向对照 + 成功路径不被污染（无横幅、无 error）。"""
        result = self._run(
            ProcessResult(exit_code=0, stdout="ok\n", stderr="", receipt=_receipt())
        )

        self.assertIsNone(result.error)
        self.assertEqual("ok", result.structured_content["status"])
        self.assertNotIn("命令失败", result.content)
        self.assertIn("ok", result.content)
        self.assertEqual(0, result.structured_content["exitCode"])

    def test_nonzero_exit_is_a_structured_failure(self) -> None:
        result = self._run(
            ProcessResult(
                exit_code=3, stdout="", stderr="oops\n", receipt=_receipt()
            )
        )

        self.assertIsNotNone(result.error)
        self.assertEqual("command_failed", result.error.code)
        self.assertTrue(result.error.retryable)
        self.assertEqual("[命令失败：退出码 3]", result.content.splitlines()[0])
        # 既有尾注保留（test_shell_sandbox 的子串契约不动）。
        self.assertIn("退出码 3", result.content)
        self.assertEqual("failed", result.structured_content["status"])
        self.assertEqual(3, result.structured_content["exitCode"])

    def test_timeout_is_failure_too(self) -> None:
        """超时也是失败 —— 否则失败感知漏掉最常见的「跑挂了」形态。"""
        result = self._run(
            ProcessResult(exit_code=None, stdout="", stderr="", receipt=_receipt())
        )

        self.assertIsNotNone(result.error)
        self.assertEqual("command_timeout", result.error.code)
        self.assertEqual("[命令失败：超时中止]", result.content.splitlines()[0])
        self.assertEqual("timeout", result.structured_content["status"])
        self.assertTrue(result.structured_content["timedOut"])

    def test_streams_are_split_into_structured_fields(self) -> None:
        result = self._run(
            ProcessResult(
                exit_code=1,
                stdout="stdout-line\n",
                stderr="stderr-line\n",
                receipt=_receipt(),
            )
        )

        self.assertEqual("stdout-line\n", result.structured_content["stdout"])
        self.assertEqual("stderr-line\n", result.structured_content["stderr"])
        self.assertFalse(result.structured_content["stdoutTruncated"])
        self.assertFalse(result.structured_content["stderrTruncated"])


class CoordinatorPerceptionTest(unittest.TestCase):
    """真 AgentRunExecutor：工具 FAILED ≠ run FAILED，模型既见输出又见归因。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.workspace = _BoundWorkspace(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_failed_command_fails_the_tool_but_not_the_run(self) -> None:
        async def _failing_runner(**kwargs) -> ShellResult:
            del kwargs
            return ShellResult(
                stdout="编译错误：缺少 main",
                stderr="cc: error\n",
                exit_code=1,
            )

        tool = RunShellTool(self.workspace.resolver, self.workspace.effect_log)
        registry = ToolRegistry()
        registry.register(tool)
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="run_shell",
                        arguments={"command": "make"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=5,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("我来修这个编译错误。"),
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
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )
        run = self.workspace.run_for()

        with patch.object(shell_tool_module, "run_shell_command", _failing_runner):
            result = asyncio.run(
                executor.execute(run.id, cancellation_token=CancellationToken())
            )

        # 工具失败≠运行失败（既有哲学不动）：run 照常收尾。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        executions = self.workspace.repository.list_tool_executions(
            result.model_turn_ids[0]
        )
        self.assertEqual(1, len(executions))
        self.assertEqual(ToolExecutionStatus.FAILED, executions[0].status)
        self.assertEqual("command_failed", executions[0].error_code)
        # 模型看到的工具消息 = 输出为主 + 失败模板尾注（不是纯模板、不是无归因输出）。
        model_visible = provider.requests[1].messages[-1].content
        self.assertIn("编译错误：缺少 main", model_visible)
        self.assertIn("工具执行失败（command_failed）", model_visible)


class _BoundWorkspace:
    """真实 resolver 夹具（与 test_shell_wall_config 同形）。"""

    def __init__(self, directory: Path) -> None:
        self.database = Database(directory / "perception-run.db")
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
        from endless_task.runtime_v2 import Actor, TranscriptEntryType

        lane = self.repository.create_lane(conversation_id=self.conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=self.conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "编译一下"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=self.conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )


if __name__ == "__main__":
    unittest.main()
