"""C4 终止与升级：run_awaiting_user 事件、升级报告与快照。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Callable, Sequence

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
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

from tests.test_run_stuck import FlakyTool, _failing_turns
from tests.test_runtime_v2_execution import ScriptedProvider


class _RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

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
        return conversation, run

    def _events(self, run_id: str, event_type: str) -> list:
        return [
            event
            for event in self.repository.list_runtime_events(run_id)
            if event.event_type == event_type
        ]

    def _executor(self, provider, tool, **kwargs) -> AgentRunExecutor:
        registry = ToolRegistry()
        registry.register(tool)
        return AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            **kwargs,
        )


class NoProgressEscalationTest(_RuntimeTestCase):
    async def test_third_consecutive_failure_escalates_once(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                *_failing_turns(4),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        executor = self._executor(provider, FlakyTool(failures=4))
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)

        escalated = self._events(run.id, "run_awaiting_user")
        # 同一原因只提请注意一次（第 3 次失败时），不会每轮刷屏。
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("no_progress", payload["reason"])
        self.assertIn("没有实质进展", payload["summary"])
        self.assertEqual(
            ["continue", "change_approach", "take_over"],
            payload["options"],
        )
        self.assertEqual(1, len(payload["repeatedFailures"]))
        self.assertEqual("read_file", payload["repeatedFailures"][0]["toolName"])
        self.assertIn("不要原样重复", payload["guidance"])
        # 报告在"第 3 次失败"这一刻生成（此后不再重复提请）。
        progress = payload["progress"]
        self.assertEqual(3, progress["toolCalls"])
        self.assertEqual(3, progress["toolFailures"])
        self.assertFalse(payload["willStop"])

    async def test_two_failures_do_not_escalate(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                *_failing_turns(2),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        executor = self._executor(provider, FlakyTool(failures=2))
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        # 2 次失败只到"卡住提示"（run.stuck），还没到升级人工。
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))
        self.assertEqual(1, len(self._events(run.id, "run_stuck")))


class BudgetEscalationTest(_RuntimeTestCase):
    def _budget_provider(self, *, input_tokens: int) -> ScriptedProvider:
        return ScriptedProvider(
            [
                (
                    ProviderTextDelta("先做点事。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=input_tokens,
                        output_tokens=0,
                    ),
                ),
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )

    async def test_budget_pressure_escalates(self) -> None:
        _, run = self._create_run()
        # 可用上限 = 1000 - 100 = 900；900/900 = 100% ≥ 85% → 升级。
        executor = self._executor(
            self._budget_provider(input_tokens=900),
            FlakyTool(failures=0),
            context_window_tokens=1000,
            max_output_tokens=100,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("budget_exhausted", payload["reason"])
        self.assertIn("900", payload["summary"])
        self.assertEqual(1000, payload["budget"]["limitTokens"] + 100)
        self.assertEqual(1.0, payload["budget"]["usedRatio"])

    async def test_budget_below_threshold_does_not_escalate(self) -> None:
        _, run = self._create_run()
        executor = self._executor(
            self._budget_provider(input_tokens=100),
            FlakyTool(failures=0),
            context_window_tokens=1000,
            max_output_tokens=100,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))

    async def test_custom_ratio_is_respected(self) -> None:
        _, run = self._create_run()
        executor = self._executor(
            self._budget_provider(input_tokens=450),
            FlakyTool(failures=0),
            context_window_tokens=1000,
            max_output_tokens=100,
            escalation_budget_ratio=0.5,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(self._events(run.id, "run_awaiting_user")))


class SafeStopEscalationTest(_RuntimeTestCase):
    async def test_no_progress_stop_reports_will_stop(self) -> None:
        _, run = self._create_run()
        turns = [
            (
                ProviderTextDelta(f"读取{index}。"),
                ProviderToolCall(
                    id=f"call_{index}",
                    name="read_file",
                    arguments={"path": "a.txt"},
                ),
                ProviderCompleted(finish_reason="tool_calls"),
            )
            for index in range(3)
        ]
        turns.append((ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")))
        executor = self._executor(
            ScriptedProvider(turns),
            FlakyTool(failures=0),
            no_progress_enforcement_enabled=True,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertIn("安全停止", result.content)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        self.assertEqual("no_progress", escalated[0].payload["reason"])
        self.assertTrue(escalated[0].payload["willStop"])


class StallingEscalationProvider:
    """失败三轮后挂住，便于在运行中读取升级快照。"""

    name = "stalling-escalation"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.reached_stall = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        cancellation_token.raise_if_cancelled()
        if not self.responses:
            self.reached_stall.set()
            await self.release.wait()
            yield ProviderTextDelta("完成")
            yield ProviderCompleted(finish_reason="stop")
            return
        for event in self.responses.pop(0):
            cancellation_token.raise_if_cancelled()
            yield event


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class EscalationSnapshotTest(_RuntimeTestCase):
    async def test_snapshot_exposes_escalation_while_running(self) -> None:
        provider = StallingEscalationProvider(_failing_turns(3))
        registry = ToolRegistry()
        registry.register(FlakyTool(failures=3))
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
        )
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "任务")
        await _wait_until(
            lambda: bool(
                [
                    event
                    for event in self.repository.list_runtime_events(handle.run_id)
                    if event.event_type == "run_awaiting_user"
                ]
            )
        )
        snapshot = gateway.snapshot(conversation.id)
        escalation = snapshot.get("escalation")
        self.assertIsNotNone(escalation)
        assert isinstance(escalation, dict)
        self.assertEqual("no_progress", escalation["reason"])
        self.assertEqual(3, escalation["progress"]["toolFailures"])
        self.assertEqual(
            ["continue", "change_approach", "take_over"],
            escalation["options"],
        )
        self.assertIsNotNone(snapshot.get("stuck"))

        provider.release.set()
        await gateway.shutdown()


if __name__ == "__main__":
    unittest.main()
