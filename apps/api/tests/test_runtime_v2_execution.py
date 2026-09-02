from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.fake_provider import FakeProvider
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
    ContextCompactionResult,
    ContextProjection,
    ModelTurnStatus,
    RunStatus,
    RuntimeV2ReplayService,
    SafetyStopPolicy,
    StaticToolApprovalGate,
    ToolApprovalDecision,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class PausedProvider:
    name = "paused"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.paused = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderTextDelta("部分")
        self.paused.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class FakeTool:
    def __init__(
        self,
        name: str = "read_file",
        *,
        content: str = "file content",
        delay: float = 0.0,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        approval_mode: ToolApprovalMode = ToolApprovalMode.AUTO,
    ) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fake {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=effect,
            approval_mode=approval_mode,
            timeout_seconds=2.0,
        )
        self.content = content
        self.delay = delay
        self.calls: list[ToolCall] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        self.started_at.append(asyncio.get_running_loop().time())
        await asyncio.sleep(self.delay)
        self.finished_at.append(asyncio.get_running_loop().time())
        return ToolResult(tool_call_id=call.id, content=self.content)

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class ReadOnlyToolWithoutConfirmationJudge:
    """读工具的真实形态：不定义 requires_explicit_confirmation（继承自 Protocol）。"""

    def __init__(self, name: str = "list_workspace_dir") -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fake {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=2.0,
        )
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content="listing")


class PausedTool(FakeTool):
    def __init__(self) -> None:
        super().__init__("paused_read")
        self.paused = asyncio.Event()

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        self.calls.append(call)
        self.paused.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class FailingTool(FakeTool):
    def __init__(self) -> None:
        super().__init__("failing_read")

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        raise ToolError(
            "tool_failed",
            "模拟工具失败。",
            retryable=True,
        )


class StaticCompactionHook:
    def __init__(self) -> None:
        self.calls: list[tuple[ProviderMessage, ...]] = []

    async def compact(self, run, messages):
        del run
        self.calls.append(tuple(messages))
        # 纯消息替换语义:不固化 summary entry,直接给出替换后的完整消息。
        return ContextCompactionResult(
            messages=(ProviderMessage(role="system", content="压缩摘要"),),
            summary_entry_id=None,
            covered_entry_ids=(),
        )


class RuntimeV2ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, lane, trigger, run

    def test_run_executes_multiple_model_turns_and_persists_final_entry(self) -> None:
        _, lane, trigger, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("我先读取文件。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=11,
                        output_tokens=4,
                    ),
                ),
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=13,
                        output_tokens=6,
                    ),
                ),
            ]
        )
        tool = FakeTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("我先读取文件。文件内容是 file content。", result.content)
        self.assertEqual(24, result.input_tokens)
        self.assertEqual(10, result.output_tokens)
        self.assertEqual(2, len(result.model_turn_ids))
        self.assertEqual(2, len(provider.requests))

        self.assertEqual("user", provider.requests[0].messages[0].role)
        self.assertEqual("读取文件", provider.requests[0].messages[0].content)
        self.assertEqual("assistant", provider.requests[1].messages[1].role)
        self.assertEqual("我先读取文件。", provider.requests[1].messages[1].content)
        self.assertEqual("call_1", provider.requests[1].messages[1].tool_calls[0].id)
        self.assertEqual("tool", provider.requests[1].messages[2].role)
        self.assertEqual("file content", provider.requests[1].messages[2].content)

        entries = self.repository.list_entries(lane.id)
        self.assertEqual(
            (
                TranscriptEntryType.USER_MESSAGE,
                TranscriptEntryType.TOOL_CALL,
                TranscriptEntryType.TOOL_RESULT,
                TranscriptEntryType.ASSISTANT_MESSAGE,
            ),
            tuple(entry.type for entry in entries),
        )
        self.assertEqual(trigger.id, entries[1].parent_id)
        self.assertEqual(result.assistant_entry_id, entries[-1].id)
        self.assertEqual(run.id, entries[-1].source_run_id)
        self.assertEqual(
            ModelTurnStatus.COMPLETED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.COMPLETED, tool_execution.status)
        self.assertIsNotNone(tool_execution.result_entry_id)

        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.COMPLETED, replay.derived_status)
        self.assertEqual(result.content, replay.partial_content)
        self.assertEqual((), replay.warnings)
        self.assertEqual(
            (ModelTurnStatus.COMPLETED, ModelTurnStatus.COMPLETED),
            tuple(turn.derived_status for turn in replay.model_turns),
        )

    def test_read_only_tool_without_confirmation_judge_completes(self) -> None:
        # 回归：只读工具（如 read_workspace_file / list_workspace_dir）不定义
        # requires_explicit_confirmation（继承自 RegisteredTool Protocol），v2 曾因
        # 无条件调用而抛 AttributeError，导致整个 run 变成 runtime_internal_error。
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("先列出目录。"),
                    ProviderToolCall(
                        id="call_1",
                        name="list_workspace_dir",
                        arguments={},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=8,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("目录已列出。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=5,
                    ),
                ),
            ]
        )
        tool = ReadOnlyToolWithoutConfirmationJudge()
        self.assertFalse(hasattr(tool, "requires_explicit_confirmation"))
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("先列出目录。目录已列出。", result.content)
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.COMPLETED, tool_execution.status)
        self.assertIsNotNone(tool_execution.result_entry_id)
        self.assertEqual(1, len(tool.calls))

        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.COMPLETED, replay.derived_status)
        self.assertEqual((), replay.warnings)

    def test_max_model_turns_safety_stop_persists_terminal_state(self) -> None:
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "one.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderToolCall(
                        id="call_2",
                        name="read_file",
                        arguments={"path": "two.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
            ]
        )
        tool = FakeTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_model_turns=2,
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("max_model_turns", self.repository.get_run(run.id).error_code)
        self.assertEqual(2, len(result.model_turn_ids))
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(5, len(self.repository.list_entries(lane.id)))

    def test_parallel_tool_batch_preserves_source_order(self) -> None:
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_slow",
                        name="slow_read",
                        arguments={"path": "slow.txt"},
                    ),
                    ProviderToolCall(
                        id="call_fast",
                        name="fast_read",
                        arguments={"path": "fast.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("两个工具都完成了。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=5),
                ),
            ]
        )
        slow = FakeTool("slow_read", content="slow result", delay=0.05)
        fast = FakeTool("fast_read", content="fast result")
        registry = ToolRegistry()
        registry.register(slow)
        registry.register(fast)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(slow.calls))
        self.assertEqual(1, len(fast.calls))
        self.assertLessEqual(
            slow.started_at[0],
            fast.finished_at[0],
            "read-only tools should execute concurrently",
        )
        tool_messages = provider.requests[1].messages[2:4]
        self.assertEqual(("call_slow", "call_fast"), tuple(
            message.tool_call_id for message in tool_messages
        ))
        entries = self.repository.list_entries(lane.id)
        tool_results = [entry for entry in entries if entry.type is TranscriptEntryType.TOOL_RESULT]
        self.assertEqual(("slow result", "fast result"), tuple(
            entry.payload["content"] for entry in tool_results
        ))

    def test_side_effect_tool_waits_for_approval(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_write",
                        name="write_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        tool = FakeTool(
            "write_file",
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.WAITING_APPROVAL, result.status)
        self.assertEqual(0, len(tool.calls))
        self.assertEqual(RunStatus.WAITING_APPROVAL, self.repository.get_run(run.id).status)
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.WAITING_APPROVAL, tool_execution.status)

    def test_approved_side_effect_tool_executes(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_write",
                        name="write_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("已写入。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=3),
                ),
            ]
        )
        tool = FakeTool(
            "write_file",
            content="written",
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(tool.calls))
        self.assertEqual(
            ToolExecutionStatus.COMPLETED,
            self.repository.list_tool_executions(result.model_turn_ids[0])[0].status,
        )

    def test_steering_is_injected_at_next_model_turn(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("已结合补充信息。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=4),
                ),
            ]
        )
        tool = FakeTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            while not provider.requests:
                await asyncio.sleep(0)
            self.assertTrue(await executor.enqueue_user_message("补充：只看摘要"))
            result = await task
            self.assertFalse(await executor.enqueue_user_message("后续问题"))
            follow_up_messages = await executor.drain_follow_up_messages()
            return result, follow_up_messages

        result, follow_up_messages = asyncio.run(scenario())
        steering_message = provider.requests[1].messages[-1]
        self.assertEqual("user", steering_message.role)
        self.assertEqual("补充：只看摘要", steering_message.content)
        self.assertEqual(RunStatus.COMPLETED, result.status)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("steer_injected", event_types)

        self.assertEqual(("后续问题",), follow_up_messages)

    def test_cancellation_persists_run_and_model_turn_state(self) -> None:
        _, _, _, run = self._create_run()
        provider = PausedProvider()
        registry = ToolRegistry()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="paused-model",
        )
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            await provider.paused.wait()
            await executor.request_cancel()
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(RunStatus.CANCELLED, result.status)
        self.assertEqual(RunStatus.CANCELLED, self.repository.get_run(run.id).status)
        self.assertEqual(
            ModelTurnStatus.CANCELLED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("run_cancel_requested", event_types)
        self.assertIn("run_cancelled", event_types)

    def test_duplicate_tool_signature_safety_stop(self) -> None:
        _, _, _, run = self._create_run()
        repeated_call = ProviderToolCall(
            id="call_1",
            name="read_file",
            arguments={"path": "a.txt"},
        )
        provider = ScriptedProvider(
            [
                (
                    repeated_call,
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    repeated_call,
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=12, output_tokens=2),
                ),
            ]
        )
        tool = FakeTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("duplicate_tool_signature", result.run.error_code)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("safety_stop", event_types)

    def test_cancellation_during_tool_execution_persists_terminal_tool_state(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_paused",
                        name="paused_read",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        tool = PausedTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            await tool.paused.wait()
            await executor.request_cancel()
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(RunStatus.CANCELLED, result.status)
        self.assertEqual(
            ModelTurnStatus.CANCELLED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.CANCELLED, tool_execution.status)
        self.assertIsNotNone(tool_execution.result_entry_id)
        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.CANCELLED, replay.derived_status)
        self.assertEqual(ModelTurnStatus.CANCELLED, replay.model_turns[0].derived_status)
        self.assertEqual(
            ToolExecutionStatus.CANCELLED,
            replay.model_turns[0].tool_executions[0].derived_status,
        )
        self.assertEqual((), replay.warnings)

    def test_no_progress_safety_stop(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (ProviderCompleted(finish_reason="stop"),),
                (ProviderCompleted(finish_reason="stop"),),
            ]
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            safety_policy=SafetyStopPolicy(max_consecutive_empty_model_turns=2),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("no_progress", result.run.error_code)

    def test_consecutive_tool_failures_safety_stop(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id=f"call_{index}",
                        name="failing_read",
                        arguments={"path": f"{index}.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                )
                for index in range(1, 4)
            ]
        )
        tool = FailingTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("consecutive_tool_failures", result.run.error_code)
        self.assertEqual(3, len(tool.calls))

    def test_provider_error_terminates_run_with_safe_error(self) -> None:
        _, _, _, run = self._create_run()
        provider = FakeProvider(
            chunks=("部分输出",),
            failure_after_chunks=1,
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="fake-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("provider_unavailable", result.run.error_code)
        self.assertEqual("模拟模型服务暂不可用。", result.run.safe_message)
        self.assertEqual(
            ModelTurnStatus.FAILED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.FAILED, replay.derived_status)
        self.assertEqual("部分输出", replay.partial_content)
        self.assertEqual(ModelTurnStatus.FAILED, replay.model_turns[0].derived_status)
        self.assertEqual((), replay.warnings)

    def test_context_compaction_hook_runs_between_model_turns(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(finish_reason="stop", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        compaction_hook = StaticCompactionHook()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            compaction_hook=compaction_hook,
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(compaction_hook.calls))
        self.assertEqual("压缩摘要", provider.requests[0].messages[0].content)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("compaction_started", event_types)
        self.assertIn("compaction_completed", event_types)


class ContextProjectionTest(unittest.TestCase):
    def test_projects_only_policy_allowed_entries(self) -> None:
        from endless_task.runtime_v2 import TranscriptEntryRecord, TranscriptEntryStatus

        def entry(
            entry_id: str,
            entry_type: TranscriptEntryType,
            actor: Actor,
            payload: dict,
            policy: dict,
        ) -> TranscriptEntryRecord:
            return TranscriptEntryRecord(
                id=entry_id,
                conversation_id="conversation",
                parent_id=None,
                lane_id="lane",
                seq=1,
                type=entry_type,
                type_version=1,
                actor=actor,
                status=TranscriptEntryStatus.FINAL,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                payload=payload,
                context_policy=policy,
                display={},
            )

        result = ContextProjection().project(
            [
                entry(
                    "user",
                    TranscriptEntryType.USER_MESSAGE,
                    Actor.USER,
                    {"content": "问题"},
                    {"include_in_llm": True, "transform": "full"},
                ),
                entry(
                    "artifact",
                    TranscriptEntryType.ARTIFACT_REF,
                    Actor.SYSTEM,
                    {"artifactId": "artifact"},
                    {"include_in_llm": False, "transform": "none"},
                ),
                entry(
                    "tool",
                    TranscriptEntryType.TOOL_RESULT,
                    Actor.TOOL,
                    {"callId": "call_1", "toolName": "read_file", "content": "结果"},
                    {
                        "include_in_llm": True,
                        "transform": "tool_result",
                        "trust_level": "untrusted",
                    },
                ),
            ]
        )

        self.assertEqual(("user", "tool"), result.included_entry_ids)
        self.assertEqual(("artifact",), result.skipped_entry_ids)
        self.assertEqual(("user", "tool"), tuple(message.role for message in result.messages))
        self.assertEqual("tool", result.messages[1].role)
        self.assertEqual("call_1", result.messages[1].tool_call_id)
