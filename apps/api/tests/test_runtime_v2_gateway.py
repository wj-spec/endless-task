from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Callable, Sequence

from endless_task.domain.models import ConversationKind
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime.provider_manager import SelectedProvider
from endless_task.runtime_v2 import (
    Actor,
    LaneKind,
    ModelTurnStatus,
    RunStatus,
    RuntimeV2SessionGateway,
    ToolApprovalDecision,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.storage.sqlite_provider_profile_repository import (
    SqliteProviderProfileRepository,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "scripted"

    def __init__(
        self,
        responses: Sequence[Sequence[ProviderStreamEvent]],
        *,
        name: str = "scripted",
    ) -> None:
        self.name = name
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        cancellation_token.raise_if_cancelled()
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event


class BlockingProvider:
    name = "counting"

    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        self.started += 1
        if self.started == 1:
            await self.release.wait()
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("完成")
        yield ProviderCompleted(
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
        )


class FailingProvider:
    name = "failing"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("部分输出")
        raise ProviderError(
            "provider_unavailable",
            "模拟服务暂时不可用。",
            retryable=True,
        )


class FakeTool:
    def __init__(self, *, approval_mode: ToolApprovalMode = ToolApprovalMode.AUTO) -> None:
        self.definition = ToolDefinition(
            name="read_file",
            description="读取测试文件",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=approval_mode,
            timeout_seconds=1.0,
        )

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


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class RuntimeV2GatewayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _gateway(
        self,
        provider,
        *,
        provider_slot_limit: int | None = None,
        context_prefix_messages: Sequence[ProviderMessage] = (),
        provider_resolver=None,
    ) -> RuntimeV2SessionGateway:
        registry = ToolRegistry()
        registry.register(FakeTool())
        return RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
            provider_slot_limit=provider_slot_limit,
            context_prefix_messages=context_prefix_messages,
            provider_resolver=provider_resolver,
        )

    async def test_gateway_injects_prefix_and_invokes_completion_callback(
        self,
    ) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=3,
                        output_tokens=2,
                    ),
                )
            ]
        )
        gateway = self._gateway(
            provider,
            context_prefix_messages=(
                ProviderMessage(role="system", content="v2 产品上下文"),
            ),
        )
        completed_runs: list[str] = []
        completed = asyncio.Event()

        async def callback(run_id: str) -> None:
            completed_runs.append(run_id)
            completed.set()

        gateway.set_run_completion_callback(callback)
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "你好")
        await asyncio.wait_for(completed.wait(), timeout=2)

        self.assertEqual(handle.run_id, completed_runs[0])
        self.assertEqual("v2 产品上下文", provider.requests[0].messages[0].content)
        self.assertEqual("scripted-model", provider.requests[0].model)
        await gateway.shutdown()

    async def test_message_submission_is_idempotent_while_run_is_active(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()

        first, duplicate = await asyncio.gather(
            gateway.send(
                conversation.id,
                "只提交一次",
                client_request_id="request-once",
            ),
            gateway.send(
                conversation.id,
                "只提交一次",
                client_request_id="request-once",
            ),
        )
        await _wait_until(lambda: provider.started == 1)

        self.assertEqual(first, duplicate)
        self.assertEqual(1, len(self.repository.list_runs(conversation_id=conversation.id)))
        self.assertEqual(
            1,
            len(
                [
                    entry
                    for entry in self.repository.list_entries(first.lane_id)
                    if entry.type is TranscriptEntryType.USER_MESSAGE
                ]
            ),
        )
        with self.assertRaises(ConflictError):
            await gateway.send(
                conversation.id,
                "不同内容",
                client_request_id="request-once",
            )

        provider.release.set()
        await _wait_until(
            lambda: self.repository.get_run(first.run_id).status
            is RunStatus.COMPLETED
        )

    async def test_failed_message_replay_returns_original_handle(self) -> None:
        provider = FailingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()

        first = await gateway.send(
            conversation.id,
            "失败后重放",
            client_request_id="failed-request",
        )
        await _wait_until(
            lambda: self.repository.get_run(first.run_id).status
            is RunStatus.FAILED
        )
        replayed = await gateway.send(
            conversation.id,
            "失败后重放",
            client_request_id="failed-request",
        )

        self.assertEqual(first, replayed)
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(
            1,
            len(self.repository.list_runs(conversation_id=conversation.id)),
        )
        self.assertEqual(
            1,
            len(
                [
                    entry
                    for entry in self.repository.list_entries(first.lane_id)
                    if entry.type is TranscriptEntryType.USER_MESSAGE
                ]
            ),
        )

    async def test_conversation_provider_and_model_are_resolved_for_v2_run(self) -> None:
        default_provider = ScriptedProvider([])
        selected_provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("使用会话配置"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=2,
                        output_tokens=2,
                    ),
                )
            ],
            name="conversation-provider",
        )
        profile_repository = SqliteProviderProfileRepository(self.database)
        profile = profile_repository.ensure_builtin_profile(
            name="environment",
            default_model="default-model",
            base_url="",
            api_key_ref="",
            timeout_seconds=60,
        )
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.set_conversation_model(
            conversation.id,
            provider_profile_id=profile.id,
            model_override="conversation-model",
        )
        resolved_conversations = []

        def resolve(current):
            resolved_conversations.append(current)
            return SelectedProvider(
                provider=selected_provider,
                model=current.model_override or profile.default_model,
                profile=profile,
            )

        gateway = self._gateway(
            default_provider,
            provider_resolver=resolve,
        )
        handle = await gateway.send(
            conversation.id,
            "使用哪个模型",
            client_request_id="provider-selection",
        )
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

        self.assertEqual(profile.id, resolved_conversations[0].provider_profile_id)
        self.assertEqual("conversation-model", selected_provider.requests[0].model)
        self.assertEqual([], default_provider.requests)
        model_turn = self.repository.list_model_turns(handle.run_id)[0]
        self.assertEqual("conversation-provider", model_turn.provider)
        self.assertEqual("conversation-model", model_turn.model)

    async def test_snapshot_has_no_write_side_effect_and_run_is_replayable(self) -> None:
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
                        input_tokens=10,
                        output_tokens=4,
                    ),
                ),
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=12,
                        output_tokens=6,
                    ),
                ),
            ]
        )
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()

        initial_snapshot = gateway.snapshot(conversation.id)
        self.assertIsNone(initial_snapshot["activeLaneId"])
        self.assertIsNone(self.repository.get_conversation_pointer(conversation.id))

        handle = await gateway.send(conversation.id, "读取文件")
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

        snapshot = gateway.snapshot(conversation.id)
        self.assertEqual(handle.lane_id, snapshot["activeLaneId"])
        self.assertEqual(handle.run_id, snapshot["activeRunId"])
        self.assertEqual(handle.run_id, snapshot["runState"]["runId"])
        self.assertEqual("completed", snapshot["runState"]["status"])
        self.assertEqual(1, len(snapshot["toolStates"]))
        self.assertEqual(
            (
                TranscriptEntryType.USER_MESSAGE,
                TranscriptEntryType.TOOL_CALL,
                TranscriptEntryType.TOOL_RESULT,
                TranscriptEntryType.ASSISTANT_MESSAGE,
            ),
            tuple(entry["type"] for entry in snapshot["entries"]),
        )

        first_events = gateway.project_events(conversation.id)
        second_events = gateway.project_events(conversation.id)
        self.assertEqual(
            tuple(event.id for event in first_events),
            tuple(event.id for event in second_events),
        )
        event_types = {event.event_type for event in first_events}
        self.assertIn("run.started", event_types)
        self.assertIn("message.updated", event_types)
        self.assertIn("tool_execution.started", event_types)
        self.assertIn("run.finished", event_types)

    async def test_temporary_conversation_inherits_frozen_base_context(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("临时回复"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=8,
                        output_tokens=3,
                    ),
                )
            ]
        )
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "继承的主线历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        visible_branch = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        branch_leaf = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=visible_branch.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "仅分支可见的历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        with self.assertRaisesRegex(ConflictError, "current main lane"):
            await gateway.create_temporary_conversation(
                source_conversation_id=conversation.id,
                source_lane_id=visible_branch.id,
                source_leaf_entry_id=branch_leaf.id,
            )

        temporary_conversation_id, temporary_lane = await gateway.create_temporary_conversation(
            source_conversation_id=conversation.id,
            source_lane_id=main.id,
            source_leaf_entry_id=base.id,
            title="临时探索",
        )
        self.assertNotEqual(conversation.id, temporary_conversation_id)
        self.assertEqual(
            ConversationKind.EPHEMERAL,
            self.chat_repository.get_conversation(temporary_conversation_id).kind,
        )
        self.assertEqual(LaneKind.TEMPORARY, temporary_lane.kind)
        provenance = self.repository.get_temporary_conversation(
            temporary_conversation_id
        )
        self.assertEqual(conversation.id, provenance.source_conversation_id)
        self.assertEqual(main.id, provenance.source_lane_id)
        self.assertEqual(base.id, provenance.source_leaf_entry_id)
        self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "fork 之后的主线消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )

        handle = await gateway.send(
            temporary_conversation_id,
            "临时问题",
            lane_id=temporary_lane.id,
        )
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

        request = provider.requests[0]
        contents = [message.content for message in request.messages]
        self.assertIn("继承的主线历史", contents)
        self.assertIn("临时问题", contents)
        self.assertNotIn("fork 之后的主线消息", contents)
        source_events = [
            event.event_type
            for event in gateway.project_events(conversation.id)
            if event.event_type.startswith("branch.")
            or event.event_type.startswith("temporary_conversation.")
        ]
        temporary_events = [
            event.event_type
            for event in gateway.project_events(temporary_conversation_id)
            if event.event_type.startswith("temporary_conversation.")
        ]
        self.assertEqual((), tuple(source_events))
        self.assertEqual(("temporary_conversation.created",), tuple(temporary_events))

    async def test_branch_rejects_incomplete_turn_boundary(self) -> None:
        gateway = self._gateway(ScriptedProvider([]))
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        user_entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "尚未完成回答的问题"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )

        with self.assertRaisesRegex(
            InvalidStateError,
            "final assistant response boundary",
        ):
            await gateway.create_lane_branch(
                conversation_id=conversation.id,
                source_lane_id=main.id,
                base_entry_id=user_entry.id,
            )

    async def test_promote_lane_switches_pointer_and_projects_event(self) -> None:
        provider = ScriptedProvider([])
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        question = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "主线回复"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=question.id,
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        branch = await gateway.create_lane_branch(
            conversation_id=conversation.id,
            source_lane_id=main.id,
            base_entry_id=base.id,
            kind=LaneKind.PERSISTENT_BRANCH,
        )

        result = await gateway.promote_lane(
            conversation_id=conversation.id,
            target_lane_id=branch.lane.id,
        )

        self.assertEqual(LaneKind.MAIN, result.promoted_lane.kind)
        self.assertEqual(LaneKind.PERSISTENT_BRANCH, result.previous_main_lane.kind)
        self.assertEqual(
            branch.lane.id,
            self.repository.get_conversation_pointer(conversation.id).active_lane_id,
        )
        self.assertEqual(
            {main.id: LaneKind.PERSISTENT_BRANCH, branch.lane.id: LaneKind.MAIN},
            {
                lane.id: lane.kind
                for lane in self.repository.list_lanes(conversation.id)
            },
        )
        with self.assertRaises(InvalidStateError):
            await gateway.rename_lane(branch.lane.id, "当前主线")
        with self.assertRaises(InvalidStateError):
            await gateway.archive_lane(branch.lane.id)

        renamed_previous_main = await gateway.rename_lane(main.id, "原主线分支")
        self.assertEqual("原主线分支", renamed_previous_main.display_name)
        archived_previous_main = await gateway.archive_lane(main.id)
        self.assertEqual((main.id,), tuple(lane.id for lane in archived_previous_main))
        self.assertTrue(archived_previous_main[0].is_archived)
        current_main = self.repository.get_lane(branch.lane.id)
        self.assertEqual(LaneKind.MAIN, current_main.kind)
        self.assertFalse(current_main.is_archived)
        self.assertEqual(
            branch.lane.id,
            self.repository.get_conversation_pointer(conversation.id).active_lane_id,
        )

        branch_events = [
            event.event_type
            for event in gateway.project_events(conversation.id)
            if event.event_type.startswith("branch.")
        ]
        self.assertEqual(
            (
                "branch.created",
                "branch.promoted",
                "branch.renamed",
                "branch.archived",
            ),
            tuple(branch_events),
        )

    async def test_branch_run_does_not_change_main_lane_pointer(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        question = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "主线回复"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=question.id,
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        branch = await gateway.create_lane_branch(
            conversation_id=conversation.id,
            source_lane_id=main.id,
            base_entry_id=base.id,
        )

        handle = await gateway.send(
            conversation.id,
            "分支继续",
            lane_id=branch.lane.id,
            client_request_id="branch-message",
        )
        try:
            await _wait_until(lambda: provider.started == 1)

            pointer = self.repository.get_conversation_pointer(conversation.id)
            self.assertEqual(main.id, pointer.active_lane_id)
            self.assertIsNone(pointer.active_run_id)
            self.assertIsNone(pointer.active_run_variant_id)
            branch_snapshot = gateway.snapshot(
                conversation.id,
                lane_id=branch.lane.id,
            )
            self.assertEqual(branch.lane.id, branch_snapshot["activeLaneId"])
            self.assertEqual(main.id, branch_snapshot["mainLaneId"])
            self.assertEqual(branch.lane.id, branch_snapshot["runningLaneId"])
            self.assertEqual(handle.run_id, branch_snapshot["runningRunId"])
        finally:
            provider.release.set()
            await _wait_until(
                lambda: self.repository.get_run(handle.run_id).status
                is RunStatus.COMPLETED
            )

    async def test_regenerate_creates_sibling_and_reuses_trigger_context(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("第一版"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=7,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("第二版"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=8,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        first = await gateway.send(conversation.id, "重新生成这个问题")
        await _wait_until(
            lambda: self.repository.get_run(first.run_id).status
            is RunStatus.COMPLETED
        )

        result = await gateway.regenerate_run(first.run_id)
        await _wait_until(
            lambda: self.repository.get_run(result.new_run_id).status
            is RunStatus.COMPLETED
        )

        first_run = self.repository.get_run(first.run_id)
        second_run = self.repository.get_run(result.new_run_id)
        self.assertEqual(first_run.sibling_group_id, second_run.sibling_group_id)
        self.assertEqual(first_run.trigger_entry_id, second_run.trigger_entry_id)
        self.assertFalse(first_run.is_active_variant)
        self.assertTrue(second_run.is_active_variant)
        self.assertEqual(
            first.user_message_id,
            self.repository.get_entry(second_run.assistant_entry_id).parent_id,
        )
        contents = [message.content for message in provider.requests[1].messages]
        self.assertIn("重新生成这个问题", contents)
        self.assertNotIn("第一版", contents)

        selected = await gateway.select_run_variant(first.run_id)
        self.assertTrue(selected.is_active_variant)
        self.assertFalse(
            self.repository.get_run(second_run.id).is_active_variant
        )
        pointer = self.repository.get_conversation_pointer(conversation.id)
        self.assertIsNone(pointer.active_run_id)
        self.assertIsNone(pointer.active_run_variant_id)
        self.assertEqual(
            first_run.assistant_entry_id,
            self.repository.get_lane(first.lane_id).leaf_entry_id,
        )
        event_types = [
            event.event_type
            for event in gateway.project_events(conversation.id)
            if event.event_type.startswith("run_variant.")
        ]
        self.assertEqual(("run_variant.changed",), tuple(event_types))

    async def test_regenerate_is_rejected_while_run_is_active(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "主线问题")
        await _wait_until(lambda: provider.started == 1)

        with self.assertRaises(ConflictError):
            await gateway.regenerate_run(handle.run_id)

        provider.release.set()
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

    async def test_branch_operations_are_rejected_while_run_is_active(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "主线问题")
        await _wait_until(lambda: provider.started == 1)

        with self.assertRaises(ConflictError):
            await gateway.create_lane_branch(
                conversation_id=conversation.id,
                source_lane_id=handle.lane_id,
                base_entry_id=handle.user_message_id,
                kind=LaneKind.TEMPORARY,
            )

        provider.release.set()
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

    async def test_approval_is_projected_and_resolved(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=10,
                        output_tokens=2,
                    ),
                ),
                (
                    ProviderTextDelta("已读取文件。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=12,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool(approval_mode=ToolApprovalMode.REQUIRED))
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
        )
        conversation = self.chat_repository.create_conversation()

        handle = await gateway.send(conversation.id, "读取文件")
        await _wait_until(lambda: gateway.pending_approvals(conversation.id))
        snapshot = gateway.snapshot(conversation.id)
        self.assertEqual(1, len(snapshot["pendingApprovals"]))
        approval = snapshot["pendingApprovals"][0]
        self.assertEqual(handle.run_id, approval["runId"])
        self.assertNotIn("metadata", approval)

        resolved = await gateway.resolve_approval(
            approval["id"],
            ToolApprovalDecision.APPROVE,
        )
        self.assertTrue(resolved)
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

        events = gateway.project_events(conversation.id)
        approval_events = [
            event for event in events if event.event_type.startswith("approval.")
        ]
        self.assertEqual(
            ("approval.requested", "approval.resolved"),
            tuple(event.event_type for event in approval_events),
        )
        self.assertNotIn("metadata", approval_events[0].data)
        self.assertEqual("approve", approval_events[1].data["decision"])

    async def test_provider_slot_limit_is_global_across_runs(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider, provider_slot_limit=1)
        first_conversation = self.chat_repository.create_conversation()
        second_conversation = self.chat_repository.create_conversation()
        first_handle = await gateway.send(first_conversation.id, "第一个问题")
        await _wait_until(lambda: provider.started == 1)
        second_handle = await gateway.send(second_conversation.id, "第二个问题")
        await asyncio.sleep(0.02)
        self.assertEqual(1, provider.started)
        provider.release.set()
        await _wait_until(
            lambda: self.repository.get_run(first_handle.run_id).status
            is RunStatus.COMPLETED
            and self.repository.get_run(second_handle.run_id).status
            is RunStatus.COMPLETED
        )
        self.assertEqual(2, provider.started)

    async def test_active_run_is_not_exposed_as_interrupted(self) -> None:
        provider = BlockingProvider()
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()

        handle = await gateway.send(conversation.id, "仍在正常执行的问题")
        await _wait_until(lambda: provider.started == 1)

        snapshot = gateway.snapshot(conversation.id)
        reports = gateway.recovery_reports(conversation.id)

        self.assertEqual(handle.run_id, snapshot["runningRunId"])
        self.assertEqual((), snapshot["interruptedRuns"])
        self.assertEqual((), reports)

        provider.release.set()
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

    async def test_interrupted_run_is_exposed_in_snapshot(self) -> None:
        provider = ScriptedProvider([])
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "中断的问题"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
            is_active_variant=True,
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        self.repository.start_run(run.id)

        snapshot = gateway.snapshot(conversation.id)
        reports = gateway.recovery_reports(conversation.id)

        self.assertEqual(1, len(snapshot["interruptedRuns"]))
        self.assertEqual(1, len(reports))
        self.assertEqual(run.id, reports[0].record.id)
        self.assertEqual(run.id, snapshot["interruptedRuns"][0]["runId"])

    async def test_mark_failed_recovery_finalizes_child_states(self) -> None:
        provider = ScriptedProvider([])
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "中断的问题"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
            is_active_variant=True,
        )
        turn = self.repository.create_model_turn(run_id=run.id)
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
        )
        self.repository.start_run(run.id)

        result = await gateway.resolve_recovery(run.id, retry=False)

        self.assertEqual("mark_failed", result.action)
        self.assertIsNone(result.new_run_id)
        self.assertEqual(RunStatus.FAILED, self.repository.get_run(run.id).status)
        self.assertEqual(
            ModelTurnStatus.FAILED,
            self.repository.get_model_turn(turn.id).status,
        )
        self.assertEqual(
            ToolExecutionStatus.FAILED,
            self.repository.get_tool_execution(tool.id).status,
        )
        self.assertEqual(
            (),
            gateway.recovery_reports(conversation.id),
        )

    async def test_retry_recovery_reuses_trigger_and_starts_new_run(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("重试成功。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=7,
                        output_tokens=4,
                    ),
                )
            ]
        )
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "中断的问题"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
            is_active_variant=True,
        )
        self.repository.start_run(run.id)

        result = await gateway.resolve_recovery(run.id, retry=True)
        await _wait_until(
            lambda: self.repository.get_run(result.new_run_id).status
            is RunStatus.COMPLETED
        )

        old_run = self.repository.get_run(run.id)
        new_run = self.repository.get_run(result.new_run_id)
        self.assertEqual("retry", result.action)
        self.assertEqual(RunStatus.FAILED, old_run.status)
        self.assertFalse(old_run.is_active_variant)
        self.assertEqual(RunStatus.COMPLETED, new_run.status)
        self.assertTrue(new_run.is_active_variant)
        self.assertEqual(old_run.sibling_group_id, new_run.sibling_group_id)
        self.assertEqual(entry.id, new_run.trigger_entry_id)
        self.assertEqual(
            (
                TranscriptEntryType.USER_MESSAGE,
                TranscriptEntryType.ASSISTANT_MESSAGE,
            ),
            tuple(item.type for item in self.repository.list_entries(lane.id)),
        )
        self.assertEqual("user", provider.requests[0].messages[0].role)
        self.assertEqual("中断的问题", provider.requests[0].messages[0].content)


if __name__ == "__main__":
    unittest.main()
