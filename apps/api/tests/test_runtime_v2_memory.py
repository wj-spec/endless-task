from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import (
    Actor,
    LaneKind,
    MemoryScope,
    RunStatus,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2MemoryRepository,
    SqliteRuntimeV2Repository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolRegistry


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
        cancellation_token.raise_if_cancelled()
        for event in self.responses.pop(0):
            cancellation_token.raise_if_cancelled()
            yield event


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class RuntimeV2MemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "v2.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.workspace_repository = SqliteWorkspaceRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.memory_repository = SqliteRuntimeV2MemoryRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _gateway(self, provider) -> RuntimeV2SessionGateway:
        return RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            max_output_tokens=128,
            memory_repository=self.memory_repository,
        )

    async def test_memory_scope_inheritance_and_isolation(self) -> None:
        gateway = self._gateway(ScriptedProvider([]))
        workspace = self.workspace_repository.create_workspace("Memory Workspace")
        conversation = self.chat_repository.create_conversation(workspace.id)
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        self.memory_repository.create_memory(
            scope=MemoryScope.USER_GLOBAL,
            kind="fact",
            content="用户全局记忆",
            conversation_id=conversation.id,
        )
        self.memory_repository.create_memory(
            scope=MemoryScope.WORKSPACE,
            kind="preference",
            content="工作区记忆",
            conversation_id=conversation.id,
            workspace_id=workspace.id,
        )
        main_memory = gateway.create_lane_memory(
            conversation_id=conversation.id,
            lane_id=main.id,
            kind="fact",
            content="主分支记忆",
        )
        branch = await gateway.create_lane_branch(
            conversation_id=conversation.id,
            source_lane_id=main.id,
            base_entry_id=base.id,
            kind=LaneKind.PERSISTENT_BRANCH,
        )
        branch_memory = gateway.create_lane_memory(
            conversation_id=conversation.id,
            lane_id=branch.lane.id,
            kind="fact",
            content="子分支记忆",
        )
        temporary_conversation_id, temporary_lane = await gateway.create_temporary_conversation(
            source_conversation_id=conversation.id,
            source_lane_id=branch.lane.id,
            source_leaf_entry_id=base.id,
            title="临时记忆探索",
        )
        temporary_memory = gateway.create_lane_memory(
            conversation_id=temporary_conversation_id,
            lane_id=temporary_lane.id,
            kind="fact",
            content="临时记忆",
        )
        late_parent_memory = gateway.create_lane_memory(
            conversation_id=conversation.id,
            lane_id=branch.lane.id,
            kind="fact",
            content="fork 后父分支新记忆",
        )

        main_memories = gateway.list_memories(
            conversation_id=conversation.id,
            lane_id=main.id,
        )
        branch_memories = gateway.list_memories(
            conversation_id=conversation.id,
            lane_id=branch.lane.id,
        )
        temporary_memories = gateway.list_memories(
            conversation_id=temporary_conversation_id,
            lane_id=temporary_lane.id,
        )
        main_contents = {memory.content for memory in main_memories}
        branch_contents = {memory.content for memory in branch_memories}
        temporary_contents = {memory.content for memory in temporary_memories}

        self.assertIn("用户全局记忆", main_contents)
        self.assertIn("工作区记忆", main_contents)
        self.assertIn("主分支记忆", main_contents)
        self.assertNotIn("子分支记忆", main_contents)
        self.assertNotIn("临时记忆", main_contents)
        self.assertIn("子分支记忆", branch_contents)
        self.assertIn("主分支记忆", branch_contents)
        self.assertIn("用户全局记忆", temporary_contents)
        self.assertIn("工作区记忆", temporary_contents)
        self.assertIn("临时记忆", temporary_contents)
        self.assertNotIn("主分支记忆", temporary_contents)
        self.assertNotIn("子分支记忆", temporary_contents)
        self.assertNotIn(late_parent_memory.content, temporary_contents)
        promotion = gateway.create_memory_promotion(
            memory_id=temporary_memory.id,
            target_scope=MemoryScope.CONVERSATION_TREE,
        )
        resolved_promotion, resolved_memory = gateway.resolve_memory_promotion(
            promotion.id,
            accept=True,
        )

        self.assertIsNotNone(resolved_memory)
        self.assertEqual(MemoryScope.CONVERSATION_TREE, resolved_memory.scope)
        self.assertEqual(temporary_memory.id, resolved_memory.source_memory_id)
        self.assertIn(
            "临时记忆",
            {
                memory.content
                for memory in gateway.list_memories(
                    conversation_id=temporary_conversation_id,
                    lane_id=temporary_lane.id,
                )
            },
        )
        self.assertNotIn(
            "临时记忆",
            {
                memory.content
                for memory in gateway.list_memories(
                    conversation_id=conversation.id,
                    lane_id=main.id,
                )
            },
        )
        event_types = [
            event.event_type
            for event in gateway.project_events(temporary_conversation_id)
            if event.event_type.startswith("memory.")
        ]
        self.assertEqual(
            ("memory.proposal_created", "memory.scope_changed"),
            tuple(event_types),
        )

        same_workspace_conversation = self.chat_repository.create_conversation(
            workspace.id
        )
        same_workspace_lane = self.repository.create_lane(
            conversation_id=same_workspace_conversation.id
        )
        same_workspace_memories = gateway.list_memories(
            conversation_id=same_workspace_conversation.id,
            lane_id=same_workspace_lane.id,
        )
        self.assertIn(
            "工作区记忆",
            {memory.content for memory in same_workspace_memories},
        )

        other_conversation = self.chat_repository.create_conversation()
        other_lane = self.repository.create_lane(
            conversation_id=other_conversation.id
        )
        other_memories = gateway.list_memories(
            conversation_id=other_conversation.id,
            lane_id=other_lane.id,
        )
        other_contents = {memory.content for memory in other_memories}
        self.assertIn("用户全局记忆", other_contents)
        self.assertNotIn("工作区记忆", other_contents)

    def test_memory_promotion_conflict_reuses_existing_memory(self) -> None:
        gateway = self._gateway(ScriptedProvider([]))
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
        )
        source = gateway.create_lane_memory(
            conversation_id=conversation.id,
            lane_id=lane.id,
            kind="fact",
            content="用户偏好结构化回答",
        )
        existing = self.memory_repository.create_memory(
            scope=MemoryScope.CONVERSATION_TREE,
            kind="preference",
            content="用户偏好结构化回答",
            conversation_id=conversation.id,
        )

        promotion = gateway.create_memory_promotion(
            memory_id=source.id,
            target_scope=MemoryScope.CONVERSATION_TREE,
        )
        self.assertEqual(existing.id, promotion.conflict_memory_id)

        resolved_promotion, resolved_memory = gateway.resolve_memory_promotion(
            promotion.id,
            accept=True,
        )
        self.assertEqual(existing.id, resolved_promotion.resolved_memory_id)
        self.assertEqual(existing.id, resolved_memory.id)
        self.assertEqual(
            source.id,
            self.memory_repository.get_memory(source.id).id,
        )

        scope_changed = [
            event.data
            for event in gateway.project_events(conversation.id)
            if event.event_type == "memory.scope_changed"
        ][-1]
        self.assertEqual(existing.id, scope_changed["conflictMemoryId"])
        self.assertTrue(scope_changed["reusedExistingMemory"])

    async def test_visible_memories_are_injected_into_provider_context(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("收到"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=8,
                        output_tokens=2,
                    ),
                )
            ]
        )
        gateway = self._gateway(provider)
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        gateway.create_lane_memory(
            conversation_id=conversation.id,
            lane_id=main.id,
            kind="preference",
            content="回答保持简洁",
        )

        handle = await gateway.send(conversation.id, "请介绍一下当前状态")
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status
            is RunStatus.COMPLETED
        )

        self.assertEqual(1, len(provider.requests))
        system_messages = [
            message
            for message in provider.requests[0].messages
            if message.role == "system"
        ]
        self.assertEqual(1, len(system_messages))
        self.assertIn("<runtime-memory>", system_messages[0].content)
        self.assertIn("回答保持简洁", system_messages[0].content)

    async def test_run_scratch_memory_is_only_visible_to_its_run(self) -> None:
        gateway = self._gateway(ScriptedProvider([]))
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "运行中的问题"},
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
        memory = gateway.create_run_memory(
            conversation_id=conversation.id,
            run_id=run.id,
            kind="fact",
            content="Run 内部临时结论",
        )

        self.assertEqual(MemoryScope.RUN_SCRATCH, memory.scope)
        self.assertEqual(
            ("Run 内部临时结论",),
            tuple(
                item.content
                for item in gateway.list_memories(
                    conversation_id=conversation.id,
                    lane_id=lane.id,
                    run_id=run.id,
                )
            ),
        )
        self.assertEqual(
            (),
            gateway.list_memories(
                conversation_id=conversation.id,
                lane_id=lane.id,
            ),
        )


if __name__ == "__main__":
    unittest.main()
