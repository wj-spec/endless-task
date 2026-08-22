from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ResponseVariantOperation, TurnStatus
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime import (
    AssistantRuntime,
    FakeProvider,
    P0ContextBuilder,
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
    ProviderToolCall,
    RecordingEventPublisher,
    RuntimeConfiguration,
    TurnController,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeRepository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)

from test_sqlite_chat_repository import SequenceClock, SequenceIdFactory


class IncompleteProvider:
    name = "incomplete"

    async def stream(self, request, cancellation_token):
        del request
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("没有完成事件")


class ConcurrencyProbeProvider:
    name = "concurrency-probe"

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request, cancellation_token):
        del request
        cancellation_token.raise_if_cancelled()
        self.calls += 1
        call_number = self.calls
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            cancellation_token.raise_if_cancelled()
            yield ProviderTextDelta(f"回答{call_number}")
            yield ProviderCompleted(finish_reason="stop")
        finally:
            self.active -= 1


class NeverCompletingProvider:
    name = "never-completing"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def stream(self, request, cancellation_token):
        del request
        cancellation_token.raise_if_cancelled()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if False:
            yield ProviderTextDelta("")


class OneToolProvider:
    name = "one-tool"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_1",
                name="read_file",
                arguments={"file_id": "file_1"},
            )
            yield ProviderCompleted(
                finish_reason="tool_calls",
                input_tokens=4,
                output_tokens=1,
            )
            return
        yield ProviderTextDelta("根据文件：测试内容")
        yield ProviderCompleted(finish_reason="stop", input_tokens=6, output_tokens=3)


class ReadFileTool:
    definition = ToolDefinition(
        name="read_file",
        description="读取已授权文件。",
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        return ToolResult(tool_call_id=call.id, content="测试内容")


class AssistantRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.clock = SequenceClock()
        self.chat_repository = SqliteChatRepository(
            self.database,
            clock=self.clock,
            id_factory=SequenceIdFactory(),
        )
        self.runtime_repository = SqliteRuntimeRepository(
            self.database,
            clock=self.clock,
        )
        self.context_builder = P0ContextBuilder(
            self.chat_repository,
            system_prompt="你是 Endless Task。",
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _runtime(self, provider, publisher=None) -> AssistantRuntime:
        return AssistantRuntime(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            context_builder=self.context_builder,
            provider=provider,
            configuration=RuntimeConfiguration(model="fake-model"),
            event_publisher=publisher,
        )

    def _new_turn(self, content: str = "你好"):
        conversation = self.chat_repository.create_conversation()
        snapshot = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content=content,
        )
        return conversation, snapshot

    async def test_successful_stream_is_persisted_and_published_in_order(self) -> None:
        _, turn = self._new_turn()
        provider = FakeProvider(chunks=("你", "好"), input_tokens=5, output_tokens=2)
        publisher = RecordingEventPublisher()
        runtime = self._runtime(provider, publisher)

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        active = result.response_variants[0]
        self.assertEqual("你好", active.assistant_message.content)
        self.assertEqual(5, active.variant.input_tokens)
        self.assertEqual(2, active.variant.output_tokens)
        self.assertEqual(
            [
                "turn.started",
                "message.started",
                "message.delta",
                "message.delta",
                "message.completed",
                "turn.completed",
            ],
            [event.type for event in publisher.events],
        )
        self.assertEqual(
            list(range(1, 7)),
            [event.sequence for event in self.runtime_repository.list_events(turn.turn.id)],
        )
        self.assertEqual("system", provider.requests[0].messages[0].role)
        self.assertEqual("你好", provider.requests[0].messages[-1].content)

    async def test_provider_failure_preserves_partial_text_and_emits_one_terminal_event(self) -> None:
        _, turn = self._new_turn("请生成长回答")
        provider = FakeProvider(
            chunks=("第一段", "第二段"),
            failure_after_chunks=1,
            failure=ProviderError(
                "request_timeout",
                "模型响应超时，可以重试。",
                retryable=True,
            ),
        )
        publisher = RecordingEventPublisher()
        runtime = self._runtime(provider, publisher)

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.FAILED, result.turn.status)
        self.assertEqual("第一段", result.response_variants[0].assistant_message.content)
        terminal = [event for event in publisher.events if event.type.startswith("turn.")][-1]
        self.assertEqual("turn.failed", terminal.type)
        self.assertEqual("request_timeout", terminal.data["error"]["code"])
        self.assertEqual("第一段", terminal.data["partialContent"])
        self.assertNotIn("turn.completed", [event.type for event in publisher.events])

    async def test_user_cancellation_stops_provider_and_keeps_partial_text(self) -> None:
        _, turn = self._new_turn("慢慢回答")
        provider = FakeProvider(chunks=("已经开始", "不应出现"), pause_after_chunks=1)
        publisher = RecordingEventPublisher()
        runtime = self._runtime(provider, publisher)

        execution = asyncio.create_task(
            runtime.execute(
                turn_id=turn.turn.id,
                variant_id=turn.turn.active_response_variant_id,
            )
        )
        await asyncio.wait_for(provider.paused.wait(), timeout=1)
        first_cancel = await runtime.request_cancel(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )
        duplicate_cancel = await runtime.request_cancel(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )
        result = await asyncio.wait_for(execution, timeout=1)

        self.assertTrue(first_cancel)
        self.assertFalse(duplicate_cancel)
        self.assertEqual(TurnStatus.CANCELLED, result.turn.status)
        self.assertEqual("已经开始", result.response_variants[0].assistant_message.content)
        self.assertEqual("turn.cancelled", publisher.events[-1].type)
        self.assertNotIn("不应出现", result.response_variants[0].assistant_message.content)

    async def test_pre_cancelled_response_never_starts_provider(self) -> None:
        _, turn = self._new_turn()
        provider = FakeProvider()
        publisher = RecordingEventPublisher()
        runtime = self._runtime(provider, publisher)

        await runtime.request_cancel(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )
        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.CANCELLED, result.turn.status)
        self.assertEqual([], provider.requests)
        self.assertEqual(["turn.cancelled"], [event.type for event in publisher.events])

    async def test_missing_provider_completion_becomes_retryable_failure(self) -> None:
        _, turn = self._new_turn()
        publisher = RecordingEventPublisher()
        runtime = self._runtime(IncompleteProvider(), publisher)

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.FAILED, result.turn.status)
        self.assertEqual("provider_error", publisher.events[-1].data["error"]["code"])
        self.assertTrue(publisher.events[-1].data["error"]["retryable"])

    async def test_agent_timeout_stops_the_entire_turn(self) -> None:
        _, turn = self._new_turn("不要无限等待")
        provider = NeverCompletingProvider()
        publisher = RecordingEventPublisher()
        runtime = AssistantRuntime(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            context_builder=self.context_builder,
            provider=provider,
            configuration=RuntimeConfiguration(
                model="fake-model",
                agent_timeout_seconds=0.01,
            ),
            event_publisher=publisher,
        )

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.FAILED, result.turn.status)
        self.assertEqual("agent_timeout", publisher.events[-1].data["error"]["code"])
        self.assertTrue(publisher.events[-1].data["error"]["retryable"])
        self.assertTrue(provider.cancelled)

    async def test_agent_loop_persists_only_the_final_chat_response(self) -> None:
        _, turn = self._new_turn("读取文件")
        provider = OneToolProvider()
        publisher = RecordingEventPublisher()
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        runtime = AssistantRuntime(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            context_builder=self.context_builder,
            provider=provider,
            configuration=RuntimeConfiguration(model="fake-model"),
            tool_registry=registry,
            event_publisher=publisher,
        )

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual(
            "根据文件：测试内容",
            result.response_variants[0].assistant_message.content,
        )
        self.assertEqual(10, result.response_variants[0].variant.input_tokens)
        self.assertEqual(4, result.response_variants[0].variant.output_tokens)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual("tool", provider.requests[1].messages[-1].role)
        self.assertEqual(
            [
                "turn.started",
                "message.started",
                "activity.started",
                "activity.completed",
                "message.delta",
                "message.completed",
                "turn.completed",
            ],
            [event.type for event in publisher.events],
        )

    async def test_context_uses_selected_canonical_response(self) -> None:
        conversation, first = self._new_turn("第一个问题")
        first_runtime = self._runtime(FakeProvider(chunks=("回答一",)))
        await first_runtime.execute(
            turn_id=first.turn.id,
            variant_id=first.turn.active_response_variant_id,
        )

        regenerated = self.chat_repository.create_response_variant(
            turn_id=first.turn.id,
            command_request_id="regenerate-1",
            operation=ResponseVariantOperation.REGENERATE,
        )
        second_runtime = self._runtime(FakeProvider(chunks=("回答二",)))
        await second_runtime.execute(
            turn_id=first.turn.id,
            variant_id=regenerated.response_variant_id,
        )
        first_turn_events = self.runtime_repository.list_events(first.turn.id)
        self.assertEqual(
            list(range(1, len(first_turn_events) + 1)),
            [event.sequence for event in first_turn_events],
        )

        second_turn = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-2",
            content="第二个问题",
        )
        final_provider = FakeProvider(chunks=("最终回答",))
        final_runtime = self._runtime(final_provider)
        await final_runtime.execute(
            turn_id=second_turn.turn.id,
            variant_id=second_turn.turn.active_response_variant_id,
        )

        self.assertEqual(
            ["你是 Endless Task。", "第一个问题", "回答二", "第二个问题"],
            [message.content for message in final_provider.requests[0].messages],
        )

    async def test_context_too_large_fails_without_calling_provider(self) -> None:
        _, turn = self._new_turn("过长消息" * 40)
        provider = FakeProvider(chunks=("不应调用",))
        publisher = RecordingEventPublisher()
        runtime = AssistantRuntime(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            context_builder=P0ContextBuilder(
                self.chat_repository,
                system_prompt="你是 Endless Task。",
                max_context_tokens=40,
            ),
            provider=provider,
            configuration=RuntimeConfiguration(
                model="fake-model",
                max_output_tokens=10,
            ),
            event_publisher=publisher,
        )

        result = await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        self.assertEqual(TurnStatus.FAILED, result.turn.status)
        self.assertEqual([], provider.requests)
        self.assertEqual("context_too_large", publisher.events[-1].data["error"]["code"])
        self.assertFalse(publisher.events[-1].data["error"]["retryable"])

    async def test_recover_interrupted_turn_marks_it_failed_and_keeps_checkpoint(self) -> None:
        _, turn = self._new_turn()
        variant_id = turn.turn.active_response_variant_id
        self.runtime_repository.start_response(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            provider="fake",
            model="fake-model",
        )
        self.runtime_repository.start_message(turn_id=turn.turn.id, variant_id=variant_id)
        self.runtime_repository.append_text_delta(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            delta="保留内容",
            accumulated_content="保留内容",
        )
        pending_call = ToolCall(
            id="call_pending",
            conversation_id=turn.turn.conversation_id,
            turn_id=turn.turn.id,
            response_variant_id=variant_id,
            tool_name="write_note",
            arguments={"content": "draft"},
            status=ToolCallStatus.WAITING_APPROVAL,
            created_at="2026-08-22T00:00:10.000Z",
        )
        approval, _ = self.runtime_repository.prepare_tool_call(
            call=pending_call,
            definition=ToolDefinition(
                name="write_note",
                description="写入本地笔记。",
                input_schema={"type": "object"},
                effect=ToolEffect.LOCAL_WRITE,
                approval_mode=ToolApprovalMode.REQUIRED,
            ),
            approval_prompt=ToolApprovalPrompt(
                summary="保存笔记吗？",
                reason="这会修改本地数据。",
            ),
        )
        self.assertIsNotNone(approval)

        events = self.runtime_repository.recover_interrupted()
        recovered = self.chat_repository.get_turn(turn.turn.id)

        self.assertEqual(1, len(events))
        self.assertEqual("turn.failed", events[0].type)
        self.assertEqual("runtime_interrupted", events[0].data["error"]["code"])
        self.assertEqual(TurnStatus.FAILED, recovered.turn.status)
        self.assertEqual("保留内容", recovered.response_variants[0].assistant_message.content)
        self.assertIsNone(self.runtime_repository.get_pending_approval(turn.turn.id))
        with self.database.connect() as connection:
            tool_row = connection.execute(
                "SELECT status FROM tool_calls WHERE turn_id = ? AND id = ?",
                (turn.turn.id, pending_call.id),
            ).fetchone()
            approval_row = connection.execute(
                "SELECT status FROM approval_requests WHERE id = ?",
                (approval.id,),
            ).fetchone()
        self.assertEqual("cancelled", tool_row["status"])
        self.assertEqual("cancelled", approval_row["status"])

    async def test_event_replay_starts_after_requested_sequence(self) -> None:
        _, turn = self._new_turn()
        runtime = self._runtime(FakeProvider(chunks=("A", "B")))
        await runtime.execute(
            turn_id=turn.turn.id,
            variant_id=turn.turn.active_response_variant_id,
        )

        replay = self.runtime_repository.list_events(turn.turn.id, after_sequence=2)
        self.assertEqual([3, 4, 5, 6], [event.sequence for event in replay])
        self.assertEqual("message.delta", replay[0].type)
        self.assertEqual("turn.completed", replay[-1].type)

    async def test_first_terminal_state_wins(self) -> None:
        _, turn = self._new_turn()
        variant_id = turn.turn.active_response_variant_id
        self.runtime_repository.start_response(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            provider="fake",
            model="fake-model",
        )
        self.runtime_repository.start_message(turn_id=turn.turn.id, variant_id=variant_id)
        self.runtime_repository.complete_response(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            content="已经完成",
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
        )

        cancelled = self.runtime_repository.cancel_response(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            partial_content="不应覆盖",
        )
        self.assertIsNone(cancelled)
        with self.assertRaises(InvalidStateError):
            self.runtime_repository.complete_response(
                turn_id=turn.turn.id,
                variant_id=variant_id,
                content="重复完成",
                finish_reason="stop",
                input_tokens=1,
                output_tokens=1,
            )

        terminal_events = [
            event.type
            for event in self.runtime_repository.list_events(turn.turn.id)
            if event.type in ("turn.completed", "turn.failed", "turn.cancelled")
        ]
        self.assertEqual(["turn.completed"], terminal_events)

    async def test_turn_controller_deduplicates_submit_and_runs_once(self) -> None:
        conversation = self.chat_repository.create_conversation()
        provider = FakeProvider(chunks=("唯一回答",))
        runtime = self._runtime(provider)
        controller = TurnController(
            chat_repository=self.chat_repository,
            runtime=runtime,
        )

        first = await controller.submit(
            conversation_id=conversation.id,
            client_request_id="same-request",
            content="只发送一次",
        )
        duplicate = await controller.submit(
            conversation_id=conversation.id,
            client_request_id="same-request",
            content="重复网络请求",
        )
        result = await controller.wait(first)

        self.assertEqual(first, duplicate)
        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual(1, len(provider.requests))
        await controller.shutdown()

    async def test_global_model_concurrency_limit_is_enforced(self) -> None:
        first_conversation, first = self._new_turn("第一条并发请求")
        second_conversation = self.chat_repository.create_conversation()
        second = self.chat_repository.create_turn(
            conversation_id=second_conversation.id,
            client_request_id="request-2",
            content="第二条并发请求",
        )
        provider = ConcurrencyProbeProvider()
        runtime = AssistantRuntime(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            context_builder=self.context_builder,
            provider=provider,
            configuration=RuntimeConfiguration(
                model="fake-model",
                max_concurrent_model_calls=1,
            ),
        )

        first_task = asyncio.create_task(
            runtime.execute(
                turn_id=first.turn.id,
                variant_id=first.turn.active_response_variant_id,
            )
        )
        second_task = asyncio.create_task(
            runtime.execute(
                turn_id=second.turn.id,
                variant_id=second.turn.active_response_variant_id,
            )
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=1)
        await asyncio.sleep(0.01)

        self.assertEqual(1, provider.calls)
        self.assertEqual(1, provider.max_active)
        provider.release.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)

        self.assertEqual(TurnStatus.COMPLETED, first_result.turn.status)
        self.assertEqual(TurnStatus.COMPLETED, second_result.turn.status)
        self.assertEqual(2, provider.calls)
        self.assertEqual(1, provider.max_active)
        self.assertNotEqual(first_conversation.id, second_conversation.id)


if __name__ == "__main__":
    unittest.main()
