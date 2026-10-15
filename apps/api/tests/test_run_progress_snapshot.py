"""切片 10 T2 Step 1 + Step 2：长活进度可见，硬停"已安全停止"终态后仍可见。

只钉**后端投影**（前端渲染那一半按控制器裁定 R5 移交 T4）：

- ① 快照 `runState` 带 `startedAt` / `elapsedSeconds` / `budget` / `currentStep`，
  且在**一次真运行**里数值随推进而变化（不是常量）：耗时/剩余真的在走，
  当前步骤从"没有工具"变成"跑过 read_file"，轮数 1 → 2；
- ①-b 预算墙的**来源是执行器**：任务档执行器（3600s）的 run 在网关（交互档
  900s）建出的快照里必须报 3600s——网关按 run 行猜档位就是伪造；
- ② 硬停（`run_awaiting_user(willStop=true)` 紧跟 `run.finished`）之后，
  `snapshot["escalation"]` 仍然给得出"已安全停止/等你决定"（**今天恒为 None**，
  因为 `_escalation_json` 在 run 非活动时直接返回 None）；反向用例钉住
  "软阈值提醒（willStop=False）在终态后不投影"，证明不是"永远返回最后一条升级"。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
)
from endless_task.runtime_v2.run_budget import RunBudget
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


def _read_file_definition() -> ToolDefinition:
    return ToolDefinition(
        name="read_file",
        description="读取测试文件",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=1.0,
    )


class AutoReadTool:
    definition = _read_file_definition()

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        del cancellation_token
        return ToolResult(tool_call_id=call.id, content="file content")

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class BlockingReadTool:
    """一条"慢工具"：卡在 execute 里直到测试放行（长活真正卡住的样子）。"""

    definition = _read_file_definition()

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        del cancellation_token
        self.started.set()
        await self.release.wait()
        return ToolResult(tool_call_id=call.id, content="file content")

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class GatedProvider:
    """两道闸的脚本 provider：每次 stream 先停住，等测试踩点后再出内容。"""

    name = "gated"

    def __init__(self) -> None:
        self.calls = 0
        self.reached_first = asyncio.Event()
        self.reached_second = asyncio.Event()
        self.release_first = asyncio.Event()
        self.release_second = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        self.calls += 1
        cancellation_token.raise_if_cancelled()
        if self.calls == 1:
            self.reached_first.set()
            await self.release_first.wait()
            yield ProviderTextDelta("先读一下。")
            yield ProviderToolCall(
                id="call_1", name="read_file", arguments={"path": "a.txt"}
            )
            yield ProviderCompleted(
                finish_reason="tool_calls", input_tokens=10, output_tokens=2
            )
            return
        if self.calls == 2:
            self.reached_second.set()
            await self.release_second.wait()
            yield ProviderTextDelta("完成。")
            yield ProviderCompleted(
                finish_reason="stop", input_tokens=5, output_tokens=3
            )
            return
        raise AssertionError(f"Unexpected model turn #{self.calls}")


class OneTurnProvider:
    name = "one-turn"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(
            finish_reason="stop", input_tokens=1, output_tokens=1
        )


class SingleToolCallProvider:
    """第一轮发一次工具调用，第二轮收尾（配合"慢工具"用）。"""

    name = "tool-call"

    def __init__(self) -> None:
        self._used = False

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        cancellation_token.raise_if_cancelled()
        if not self._used:
            self._used = True
            yield ProviderToolCall(
                id="call_1", name="read_file", arguments={"path": "a.txt"}
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(finish_reason="stop")


class _UnusedProvider:
    """只做快照时用的占位 provider（快照路径不会调用它）。"""

    name = "unused"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request, cancellation_token
        raise AssertionError("Snapshot-only gateway must not call the provider")
        yield  # pragma: no cover - 让函数体成为生成器


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class _RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # 注册在**最前**：unittest 的 cleanup 是 LIFO，先注册 = 最后执行，
        # 这样先 await 掉网关/运行，再删临时目录（否则后台任务会撞上已删的库）。
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "任务"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
        )
        return conversation, run

    def _snapshot_gateway(self, *, run_budget: Optional[RunBudget] = None):
        """只要快照、不要执行：provider 是占位实现，run 由别的执行器跑。"""

        return RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=_UnusedProvider(),
            tool_registry=ToolRegistry(),
            model="scripted-model",
            max_output_tokens=128,
            run_budget=run_budget,
        )


class RunProgressSnapshotTest(_RuntimeTestCase):
    async def test_snapshot_progress_fields_advance_during_a_live_run(self) -> None:
        """① 四个字段齐全，且在一次真运行里**随推进变化**（不是常量）。"""
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        provider = GatedProvider()
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
            # 故意用一组"不可能是任何默认档"的墙：137 秒不是 900/3600/14400。
            run_budget=RunBudget(
                soft_seconds=5.0, hard_seconds=137.0, per_turn_seconds=9.0
            ),
        )
        self.addAsyncCleanup(gateway.shutdown)

        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "任务")

        await asyncio.wait_for(provider.reached_first.wait(), 3.0)
        first = gateway.snapshot(conversation.id)["runState"]
        stored = self.repository.get_run(handle.run_id)
        self.assertEqual(stored.started_at, first["startedAt"])
        self.assertIsNotNone(first["startedAt"])
        # ①-a：四个字段都在（`budget` 由 `_run_state_with_budget_json` 合并进来）。
        for key in ("startedAt", "elapsedSeconds", "budget", "currentStep"):
            self.assertIn(key, first)
        # ①-b：墙的数值来自执行器给入的预算，不是任何默认档、也不是常量。
        self.assertIsNotNone(
            first["budget"],
            "执行器必须把这一 run 的预算墙声明出来（否则快照只能答『不知道』）",
        )
        self.assertEqual(5.0, first["budget"]["softSeconds"])
        self.assertEqual(137.0, first["budget"]["hardSeconds"])
        self.assertEqual("run_budget_declared", first["budget"]["source"])
        # 还没跑过任何工具：当前步骤是"第 1 轮、没有工具"。
        self.assertEqual(1, first["currentStep"]["modelTurnCount"])
        self.assertIsNone(first["currentStep"]["toolName"])

        provider.release_first.set()
        await asyncio.wait_for(provider.reached_second.wait(), 3.0)
        # 让真实时间往前走一段，耗时/剩余必须跟着走（不然就是常量）。
        await asyncio.sleep(0.15)
        second = gateway.snapshot(conversation.id)["runState"]

        self.assertGreater(
            second["elapsedSeconds"],
            first["elapsedSeconds"],
            "elapsedSeconds 必须随运行推进（活值），不能在两次快照间不动",
        )
        self.assertLess(
            second["budget"]["remainingSeconds"],
            first["budget"]["remainingSeconds"],
            "剩余预算必须随耗时减少",
        )
        self.assertEqual(
            second["budget"]["hardSeconds"] - second["elapsedSeconds"],
            second["budget"]["remainingSeconds"],
        )
        # ①-c：当前步骤推进到"第 2 轮，跑过 read_file（第 1 轮）"。
        self.assertEqual(2, second["currentStep"]["modelTurnCount"])
        self.assertEqual(2, second["currentStep"]["modelTurnIndex"])
        self.assertEqual("read_file", second["currentStep"]["toolName"])
        self.assertEqual("completed", second["currentStep"]["toolStatus"])
        self.assertEqual(1, second["currentStep"]["toolTurnIndex"])

        provider.release_second.set()
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )
        finished = gateway.snapshot(conversation.id)["runState"]
        final_run = self.repository.get_run(handle.run_id)
        self.assertEqual("completed", finished["status"])
        # 终态后耗时冻结在 finished_at - started_at，不再增长。
        self.assertEqual(
            finished["elapsedSeconds"],
            round(
                (
                    datetime.fromisoformat(
                        final_run.finished_at.replace("Z", "+00:00")
                    )
                    - datetime.fromisoformat(
                        final_run.started_at.replace("Z", "+00:00")
                    )
                ).total_seconds(),
                3,
            ),
        )
        self.assertEqual(137.0, finished["budget"]["hardSeconds"])
        self.assertGreaterEqual(
            finished["elapsedSeconds"], second["elapsedSeconds"]
        )

    async def test_current_step_follows_an_in_flight_tool(self) -> None:
        """①-d：长活卡在一条慢工具上时，当前步骤就是那条工具（running）。"""
        blocking = BlockingReadTool()
        registry = ToolRegistry()
        registry.register(blocking)
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=SingleToolCallProvider(),
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
        )
        self.addAsyncCleanup(gateway.shutdown)

        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "任务")
        await asyncio.wait_for(blocking.started.wait(), 3.0)

        run_state = gateway.snapshot(conversation.id)["runState"]
        self.assertEqual("read_file", run_state["currentStep"]["toolName"])
        self.assertEqual("running", run_state["currentStep"]["toolStatus"])
        self.assertTrue(run_state["currentStep"]["toolInFlight"])

        blocking.release.set()
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

    async def test_snapshot_budget_comes_from_the_executor_not_the_gateway(
        self,
    ) -> None:
        """①-e 诚实来源：任务档执行器跑的 run，网关（交互档）也必须照实报 3600s。"""
        conversation, run = self._create_run()
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=OneTurnProvider(),
            tool_registry=registry,
            model="scripted-model",
            # 任务/提醒档（wiring 的 task_executor 就是这个档）。
            run_budget=RunBudget(
                soft_seconds=600.0, hard_seconds=3600.0, cost_cap_usd=3.0
            ),
        )
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        self.assertIs(RunStatus.COMPLETED, result.status)

        # 网关自己拿到的是交互档（900s）——它不该拿自己的档回答别人的 run。
        gateway = self._snapshot_gateway(
            run_budget=RunBudget(soft_seconds=180.0, hard_seconds=900.0)
        )
        budget = gateway.snapshot(conversation.id)["runState"]["budget"]
        self.assertIsNotNone(budget)
        self.assertEqual(600.0, budget["softSeconds"])
        self.assertEqual(3600.0, budget["hardSeconds"])
        self.assertEqual(3.0, budget["costCapUsd"])


    async def test_snapshot_budget_is_none_without_a_declared_event(self) -> None:
        """切片 10 终审修复 W-D：没有 ``run.budget_declared`` 时必须如实返回 None。

        起点 `ec997c5`：把 `_run_budget_json` 的 `declared is None` 分支改成"退回默认
        档"，**没有**任何用例会红——默认档（交互档 900s）对一个真实 run 来说就是伪造。
        这条用例把"答不上来说不知道"钉住。

        造法：只**拦掉**执行器要发的这一条声明（其余事件序完整——直接删库里的行会
        破坏 `event_seq` 连续性，replay 会先拒绝）。
        """

        class _WithoutBudgetDeclaration:
            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __init__(self, inner) -> None:
                self._inner = inner

            def append_runtime_event(self, **kwargs):
                if kwargs.get("event_type") == "run_budget_declared":
                    return None
                return self._inner.append_runtime_event(**kwargs)

        conversation, run = self._create_run()
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        executor = AgentRunExecutor(
            repository=_WithoutBudgetDeclaration(self.repository),
            provider=OneTurnProvider(),
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(soft_seconds=5.0, hard_seconds=137.0),
        )
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        self.assertIs(RunStatus.COMPLETED, result.status)
        self.assertEqual(
            [],
            [
                event
                for event in self.repository.list_runtime_events(run.id)
                if event.event_type == "run_budget_declared"
            ],
        )

        gateway = self._snapshot_gateway(
            run_budget=RunBudget(soft_seconds=180.0, hard_seconds=900.0)
        )
        run_state = gateway.snapshot(conversation.id)["runState"]
        self.assertIsNotNone(run_state, "终态 run 仍应给出 runState")
        self.assertIsNone(
            run_state["budget"],
            "没有 run_budget_declared 事件时必须答『不知道』，不许退回默认档",
        )

    async def test_a_real_run_declares_its_budget_exactly_once(self) -> None:
        """切片 10 终审修复 W-E：一次真 run 的 runtime 事件里恰好一条声明。

        审批在**同一次** `execute` 内等待，所有重跑路径都新建 run id，
        所以声明**每 run 恰好一条**。这条用例把"恰好一条"钉住。
        """
        conversation, run = self._create_run()
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=OneTurnProvider(),
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(soft_seconds=5.0, hard_seconds=137.0),
        )
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        self.assertIs(RunStatus.COMPLETED, result.status)

        declared = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_budget_declared"
        ]
        self.assertEqual(
            1, len(declared), "一次 run 恰好一条 run_budget_declared（不许按轮刷屏）"
        )
        self.assertEqual(137.0, declared[0].payload["hardSeconds"])

        gateway = self._snapshot_gateway(
            run_budget=RunBudget(soft_seconds=180.0, hard_seconds=900.0)
        )
        self.assertEqual(
            "run_budget_declared",
            gateway.snapshot(conversation.id)["runState"]["budget"]["source"],
        )


class HardStopVisibilityTest(_RuntimeTestCase):
    async def _run_until_terminal(self, *, budget: RunBudget):
        conversation, run = self._create_run()
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=OneTurnProvider(),
            tool_registry=registry,
            model="scripted-model",
            run_budget=budget,
        )
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        return conversation, run, result

    def _create_run_in_conversation(
        self, conversation_id: str, lane_id: str, *, content: str
    ):
        """同一个会话里再开一条 run（用于"旧升级之后又跑了一条 run"的场景）。"""
        trigger = self.repository.append_entry(
            conversation_id=conversation_id,
            lane_id=lane_id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": content},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=conversation_id,
            lane_id=lane_id,
            trigger_entry_id=trigger.id,
        )

    async def _drive(self, run, *, budget: RunBudget):
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=OneTurnProvider(),
            tool_registry=registry,
            model="scripted-model",
            run_budget=budget,
        )
        return await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )

    async def test_stale_hard_stop_escalation_is_not_projected_after_a_later_run(self) -> None:
        """切片 10 终审修复 W-B②：旧 run 的硬停横幅**不许**泄漏给后来的 run。

        起点 `ec997c5`：`_awaiting_user_scope` 只问"最近一条 `run.awaiting_user` 所属
        run 有没有终态事件"，不问"它是不是最近的 run"。run1 硬停（COMPLETED +
        willStop=true）→ 用户继续 → run2 宽裕预算从未升级、也 COMPLETED → 快照仍给
        `escalation.runId=run1`、`willStop=True`（对别的消费者说谎）。
        """
        conversation, run1 = self._create_run()
        lane_id = self.repository.get_run(run1.id).lane_id

        # run1：0.0005s 硬墙 → 第一轮跑完，下一轮顶部撞墙 → 优雅收尾 + 硬停升级
        first_result = await self._drive(
            run1, budget=RunBudget(soft_seconds=None, hard_seconds=0.0005)
        )
        self.assertIs(RunStatus.COMPLETED, first_result.status)
        hard_stops = [
            event.payload
            for event in self.repository.list_runtime_events(run1.id)
            if event.event_type == "run_awaiting_user"
            and event.payload["willStop"] is True
        ]
        self.assertEqual(1, len(hard_stops), "run1 必须先真的硬停过（否则本用例测不到泄漏）")

        # 用户继续：同一 lane 里再开一条 run（`idx_v2_runs_one_active_per_lane` 要求
        # run1 已经终态，所以先跑 run1 再建 run2——这也正是产品里的真实顺序）
        run2 = self._create_run_in_conversation(
            conversation.id, lane_id, content="继续（预算宽裕）"
        )

        # run2：预算宽裕，从未升级 → 没有任何 run.awaiting_user
        second_result = await self._drive(
            run2, budget=RunBudget(soft_seconds=None, hard_seconds=30.0)
        )
        self.assertIs(RunStatus.COMPLETED, second_result.status)
        self.assertEqual(
            [],
            [
                event
                for event in self.repository.list_runtime_events(run2.id)
                if event.event_type == "run_awaiting_user"
            ],
            "run2 不许升级（它是干净的后续 run）",
        )

        gateway = self._snapshot_gateway()
        snapshot = gateway.snapshot(conversation.id)
        self.assertIsNone(snapshot["runningRunId"])
        self.assertIsNone(
            snapshot["escalation"],
            "run2 正常跑完、从未升级 → 旧 run 的 willStop 横幅不许再挂出来",
        )

    async def test_a_new_run_own_escalation_is_still_projected(self) -> None:
        """切片 10 终审修复 W-B③：收窄不许变成"永远 None"。

        run1 正常跑完（从未升级）→ run2 自己硬停 → 快照必须给的是 **run2** 的升级。
        """
        conversation, run1 = self._create_run()
        lane_id = self.repository.get_run(run1.id).lane_id

        first_result = await self._drive(
            run1, budget=RunBudget(soft_seconds=None, hard_seconds=30.0)
        )
        self.assertIs(RunStatus.COMPLETED, first_result.status)
        run2 = self._create_run_in_conversation(
            conversation.id, lane_id, content="第二条（会硬停）"
        )
        second_result = await self._drive(
            run2, budget=RunBudget(soft_seconds=None, hard_seconds=0.0005)
        )
        self.assertIs(RunStatus.COMPLETED, second_result.status)

        gateway = self._snapshot_gateway()
        snapshot = gateway.snapshot(conversation.id)
        self.assertIsNone(snapshot["runningRunId"])
        escalation = snapshot["escalation"]
        self.assertIsNotNone(
            escalation, "新 run 自己的硬停升级必须照旧可见（收窄 ≠ 永远 None）"
        )
        assert isinstance(escalation, dict)
        self.assertEqual(run2.id, escalation["runId"])
        self.assertTrue(escalation["willStop"])
        self.assertEqual("time_budget_exhausted", escalation["reason"])

    async def test_will_stop_escalation_is_still_projected_after_the_run_ends(
        self,
    ) -> None:
        """② 硬停后"已安全停止/等你决定"仍可见（今天：`_escalation_json` 返回 None）。"""
        # 0.0005s 硬墙：第一轮跑完，下一轮顶部就撞墙 → 优雅收尾（COMPLETED）。
        conversation, run, result = await self._run_until_terminal(
            budget=RunBudget(soft_seconds=None, hard_seconds=0.0005)
        )
        self.assertIs(RunStatus.COMPLETED, result.status)
        self.assertIn("安全收尾", result.content)
        stored = self.repository.get_run(run.id)
        self.assertIs(RunStatus.COMPLETED, stored.status)

        gateway = self._snapshot_gateway()
        snapshot = gateway.snapshot(conversation.id)
        # 运行已经终态：快照不再报"还在跑"（这正是 T4 停止 800ms 重连的依据）。
        self.assertIsNone(snapshot["runningRunId"])
        escalation = snapshot["escalation"]
        self.assertIsNotNone(
            escalation,
            "硬停后快照必须仍给出升级报告，否则『已安全停止/等你决定』默认不可见",
        )
        assert isinstance(escalation, dict)
        self.assertEqual(run.id, escalation["runId"])
        self.assertTrue(escalation["willStop"])
        self.assertEqual("time_budget_exhausted", escalation["reason"])
        # 前端渲染"已安全停止，等你决定"所依赖的三件东西：runId 作用域、
        # willStop 位、以及可点的选项——后端必须都给得出。
        self.assertEqual(
            ["continue", "change_approach", "take_over"],
            escalation["options"],
        )
        # 摘要本身是"已到时长阈值"的中性文案（前端横幅的"已安全停止，等你决定"
        # 由 willStop 渲染）；用户可见的收尾引导语在**正文**里。
        self.assertIn("已到时长阈值", escalation["summary"])

    async def test_soft_escalation_is_not_projected_after_the_run_ends(self) -> None:
        """②-b 反向：不是"永远返回最后一条升级"——软阈值提醒终态后照旧不投影。"""
        conversation, run, result = await self._run_until_terminal(
            budget=RunBudget(soft_seconds=0.0001, hard_seconds=30.0)
        )
        self.assertIs(RunStatus.COMPLETED, result.status)
        advisories = [
            event.payload
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_awaiting_user"
            and event.payload["willStop"] is False
        ]
        self.assertEqual(1, len(advisories))

        gateway = self._snapshot_gateway()
        snapshot = gateway.snapshot(conversation.id)
        self.assertIsNone(snapshot["runningRunId"])
        self.assertIsNone(
            snapshot["escalation"],
            "软阈值提醒在运行结束后不该继续挂在快照上（横幅会永远不消失）",
        )


if __name__ == "__main__":
    unittest.main()
