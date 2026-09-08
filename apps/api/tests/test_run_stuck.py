"""C2 失败记忆的运行时集成：run.stuck 事件、失败记忆注入、快照卡住态。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Callable, Optional, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderMessage,
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
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)

from tests.test_runtime_v2_execution import ScriptedProvider


class FlakyTool:
    """前 ``failures`` 次调用失败，之后成功（用于验证失败记忆会清零）。"""

    def __init__(
        self,
        *,
        name: str = "read_file",
        failures: int = 2,
        error_code: str = "temporary_unavailable",
    ) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Flaky {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=2.0,
        )
        self.failures = failures
        self.error_code = error_code
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        if len(self.calls) <= self.failures:
            return ToolResult.failed(
                tool_call_id=call.id,
                error=ToolCallError(
                    code=self.error_code,
                    safe_message="工具暂时不可用。",
                    retryable=True,
                ),
            )
        return ToolResult(tool_call_id=call.id, content="file content")

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


def _failing_turns(count: int, *, tool_name: str = "read_file") -> list:
    return [
        (
            ProviderTextDelta(f"第 {index} 次尝试。"),
            ProviderToolCall(
                id=f"call_{index}",
                name=tool_name,
                arguments={"path": "a.txt"},
            ),
            ProviderCompleted(finish_reason="tool_calls"),
        )
        for index in range(1, count + 1)
    ]


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


class FailureMemoryRunTest(_RuntimeTestCase):
    async def test_repeated_failure_emits_run_stuck_and_injects_memory(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                *_failing_turns(3),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        tool = FlakyTool(failures=3)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)

        stuck_events = self._events(run.id, "run_stuck")
        # 第 2 次失败 → remind；第 3 次失败 → restrict（同一组失败只升级一次）。
        self.assertEqual(["remind", "restrict"], [
            event.payload.get("level") for event in stuck_events
        ])
        first = stuck_events[0].payload
        self.assertEqual("repeated_failure", first.get("detector"))
        self.assertEqual(2, first["repeatedFailures"][0]["count"])
        self.assertEqual("read_file", first["repeatedFailures"][0]["toolName"])
        self.assertEqual(
            "temporary_unavailable",
            first["repeatedFailures"][0]["errorCode"],
        )
        self.assertIn("不要原样重复", str(first.get("guidance")))

        steer_events = self._events(run.id, "steer_injected")
        failure_steers = [
            event
            for event in steer_events
            if "失败记忆" in str(event.payload.get("content"))
        ]
        # 同一组（工具，错误码）只注入一次，避免占满上下文。
        self.assertEqual(1, len(failure_steers))

    async def test_success_after_failures_resumes_progress(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                *_failing_turns(2),
                (
                    ProviderTextDelta("换一种方式。"),
                    ProviderToolCall(
                        id="call_ok",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        tool = FlakyTool(failures=2)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(self._events(run.id, "run_stuck")))
        # 同工具成功一次 → 连续失败清零 → 恢复事件，卡住态解除。
        self.assertEqual(1, len(self._events(run.id, "run_progress_resumed")))

    async def test_single_failure_is_not_stuck(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                *_failing_turns(1),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        registry = ToolRegistry()
        registry.register(FlakyTool(failures=1))
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_stuck"))


class NoProgressStuckTest(_RuntimeTestCase):
    """no-progress 评估（启用时）也走同一条 run.stuck 通道。"""

    async def test_identical_tool_repeat_emits_run_stuck_levels(self) -> None:
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
        registry = ToolRegistry()
        registry.register(FlakyTool(failures=0))
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=ScriptedProvider(turns),
            tool_registry=registry,
            model="scripted-model",
            no_progress_enforcement_enabled=True,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        stuck_events = self._events(run.id, "run_stuck")
        levels = [event.payload.get("level") for event in stuck_events]
        self.assertIn("restrict", levels)
        self.assertIn("stop", levels)
        self.assertEqual(
            "identical_tool_outcome",
            stuck_events[0].payload.get("detector"),
        )


class StallingProvider:
    """失败两轮之后挂住第三轮，便于在运行中读取快照。"""

    name = "stalling"

    def __init__(
        self,
        responses: Sequence[Sequence[ProviderStreamEvent]],
    ) -> None:
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


class StuckSnapshotTest(_RuntimeTestCase):
    def _gateway(self, provider, tool) -> RuntimeV2SessionGateway:
        registry = ToolRegistry()
        registry.register(tool)
        return RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
        )

    async def test_snapshot_exposes_stuck_state_while_running(self) -> None:
        provider = StallingProvider(_failing_turns(2))
        gateway = self._gateway(provider, FlakyTool(failures=2))
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "任务")

        await _wait_until(
            lambda: bool(
                [
                    event
                    for event in self.repository.list_runtime_events(handle.run_id)
                    if event.event_type == "run_stuck"
                ]
            )
        )
        snapshot = gateway.snapshot(conversation.id)
        stuck = snapshot.get("stuck")
        self.assertIsNotNone(stuck)
        assert isinstance(stuck, dict)
        self.assertEqual("remind", stuck["level"])
        self.assertEqual("repeated_failure", stuck["detector"])
        self.assertEqual(2, stuck["repeatedFailures"][0]["count"])
        self.assertEqual("read_file", stuck["repeatedFailures"][0]["toolName"])

        provider.release.set()
        await gateway.shutdown()

    async def test_snapshot_stuck_is_none_without_failures(self) -> None:
        provider = StallingProvider([])
        gateway = self._gateway(provider, FlakyTool(failures=0))
        conversation = self.chat_repository.create_conversation()
        await gateway.send(conversation.id, "任务")
        await _wait_until(provider.reached_stall.is_set)
        snapshot = gateway.snapshot(conversation.id)
        self.assertIsNone(snapshot.get("stuck"))
        provider.release.set()
        await gateway.shutdown()


if __name__ == "__main__":
    unittest.main()
