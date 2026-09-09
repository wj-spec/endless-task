from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime_v2 import (
    Actor,
    LaneKind,
    LaneStatus,
    ModelTurnStatus,
    RunStatus,
    ToolExecutionStatus,
    TranscriptEntryStatus,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository


class SequenceIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


class SequenceClock:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"2026-08-28T00:00:{self._value:02d}.000Z"


class SqliteRuntimeV2RepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "runtime-v2.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(
            self.database,
            clock=SequenceClock(),
            id_factory=SequenceIdFactory(),
        )
        self.repository = SqliteRuntimeV2Repository(
            self.database,
            clock=SequenceClock(),
            id_factory=SequenceIdFactory(),
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_runtime_v2_migration_is_idempotent(self) -> None:
        self.assertIn("036_runtime_v2.sql", self.database.applied_migrations())
        self.assertIn("037_runtime_v2_product_events.sql", self.database.applied_migrations())
        self.assertIn("038_runtime_v2_lane_events.sql", self.database.applied_migrations())
        self.assertIn("039_runtime_v2_memory_scopes.sql", self.database.applied_migrations())
        self.assertIn("040_runtime_v2_migration_mappings.sql", self.database.applied_migrations())
        self.assertIn("041_runtime_v2_runtime_overrides.sql", self.database.applied_migrations())
        self.assertIn(
            "042_runtime_v2_lane_lifecycle_and_temporary_conversations.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "043_runtime_v2_message_idempotency.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "044_runtime_v2_main_lane_pointer.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "045_runtime_v2_promoted_main_lane_repair.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "046_provider_model_catalog.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "047_eval.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "048_memory_auto_fact_origin.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "049_runtime_v2_migrate_v1_memories.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "050_runtime_v2_memory_quality.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "051_runtime_v2_memory_update.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "052_runtime_v2_tool_error_contract.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "053_task_run_atomic_claim.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "054_delegation_conversation_parent.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "055_runtime_ledger_trace.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "056_runtime_ledger_cost.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "057_runtime_v2_resend_override.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "058_runtime_v2_drop_runtime_overrides.sql",
            self.database.applied_migrations(),
        )
        self.database.initialize()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()
        self.assertEqual(67, row["count"])

    def test_nested_write_raises_instead_of_deadlocking(self) -> None:
        # 06 §30.5: same-thread nested _write would block the inner BEGIN
        # IMMEDIATE on the outer write lock; the reentrancy guard turns it
        # into an explicit error.
        from endless_task.domain.repositories import InvalidStateError

        def nested(connection):
            self.repository._write(
                lambda inner_connection: inner_connection.execute(
                    "SELECT 1"
                )
            )

        with self.assertRaises(InvalidStateError) as caught:
            self.repository._write(nested)
        self.assertIn("嵌套调用", str(caught.exception))

    def test_main_lane_pointer_migration_repairs_legacy_run_pointer(self) -> None:
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        promoted = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        running_branch = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=running_branch.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "分支运行"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=running_branch.id,
            trigger_entry_id=trigger.id,
        )
        self.repository.append_lane_event(
            conversation_id=conversation.id,
            lane_id=promoted.id,
            event_type="branch.promoted",
            data={"previousMainLaneId": main.id},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=running_branch.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE v2_lanes
                SET status = 'archived', archived_at = '2026-08-28T01:30:00.000Z'
                WHERE id IN (?, ?)
                """,
                (main.id, promoted.id),
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE name = ?",
                ("044_runtime_v2_main_lane_pointer.sql",),
            )

        self.database.initialize()

        pointer = self.repository.get_conversation_pointer(conversation.id)
        self.assertEqual(promoted.id, pointer.active_lane_id)
        self.assertIsNone(pointer.active_run_id)
        self.assertIsNone(pointer.active_run_variant_id)
        promoted_lane = self.repository.get_lane(promoted.id)
        previous_main_lane = self.repository.get_lane(main.id)
        self.assertEqual(LaneKind.MAIN, promoted_lane.kind)
        self.assertEqual(LaneStatus.ACTIVE, promoted_lane.status)
        self.assertEqual(LaneKind.PERSISTENT_BRANCH, previous_main_lane.kind)
        self.assertEqual(LaneStatus.ARCHIVED, previous_main_lane.status)
        self.assertEqual(
            LaneKind.PERSISTENT_BRANCH,
            self.repository.get_lane(running_branch.id).kind,
        )
        self.assertEqual(
            1,
            sum(
                lane.kind is LaneKind.MAIN
                for lane in self.repository.list_lanes(conversation.id)
            ),
        )

    def test_follow_up_migration_repairs_databases_with_old_044_applied(self) -> None:
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "旧主线"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        promoted = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=promoted.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "待提升分支"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=promoted.id,
            trigger_entry_id=trigger.id,
        )
        self.repository.append_lane_event(
            conversation_id=conversation.id,
            lane_id=promoted.id,
            event_type="branch.promoted",
            data={"previousMainLaneId": main.id},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=promoted.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE v2_lanes
                SET status = 'archived', archived_at = '2026-08-28T01:30:00.000Z'
                WHERE id IN (?, ?)
                """,
                (main.id, promoted.id),
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE name = ?",
                ("045_runtime_v2_promoted_main_lane_repair.sql",),
            )

        self.assertIn(
            "044_runtime_v2_main_lane_pointer.sql",
            self.database.applied_migrations(),
        )
        self.database.initialize()

        pointer = self.repository.get_conversation_pointer(conversation.id)
        self.assertEqual(promoted.id, pointer.active_lane_id)
        self.assertIsNone(pointer.active_run_id)
        self.assertIsNone(pointer.active_run_variant_id)
        promoted_lane = self.repository.get_lane(promoted.id)
        previous_main_lane = self.repository.get_lane(main.id)
        self.assertEqual(LaneKind.MAIN, promoted_lane.kind)
        self.assertEqual(LaneStatus.ACTIVE, promoted_lane.status)
        self.assertEqual(LaneKind.PERSISTENT_BRANCH, previous_main_lane.kind)
        self.assertEqual(LaneStatus.ARCHIVED, previous_main_lane.status)
        self.assertEqual(
            1,
            sum(
                lane.kind is LaneKind.MAIN
                for lane in self.repository.list_lanes(conversation.id)
            ),
        )
        self.assertIn(
            "045_runtime_v2_promoted_main_lane_repair.sql",
            self.database.applied_migrations(),
        )

    def test_message_submission_is_atomic_for_concurrent_duplicates(self) -> None:
        conversation = self.chat_repository.create_conversation()
        barrier = Barrier(2)

        def submit():
            barrier.wait()
            return self.repository.create_message_submission(
                conversation_id=conversation.id,
                lane_id=None,
                content="并发消息",
                client_request_id="concurrent-request",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            submissions = tuple(
                future.result()
                for future in (executor.submit(submit), executor.submit(submit))
            )

        self.assertEqual(1, sum(item.created for item in submissions))
        self.assertEqual(1, len({item.lane.id for item in submissions}))
        self.assertEqual(1, len({item.user_entry.id for item in submissions}))
        self.assertEqual(1, len({item.run.id for item in submissions}))
        self.assertEqual(
            1,
            len(self.repository.list_runs(conversation_id=conversation.id)),
        )

    def test_product_event_round_trip_is_idempotent(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "你好"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
        )
        source_event = self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={"triggerEntryId": entry.id},
        )

        product_event = self.repository.append_product_event(
            conversation_id=conversation.id,
            event_type="run.started",
            run_id=run.id,
            lane_id=lane.id,
            source_event_id=source_event.event_id,
            occurred_at=source_event.occurred_at,
            data={"runId": run.id, "status": "running"},
        )
        repeated = self.repository.append_product_event(
            conversation_id=conversation.id,
            event_type="run.started",
            run_id=run.id,
            lane_id=lane.id,
            source_event_id=source_event.event_id,
            occurred_at=source_event.occurred_at,
            data={"runId": run.id, "status": "running"},
        )

        self.assertEqual(product_event.id, repeated.id)
        self.assertEqual(1, product_event.event_seq)
        self.assertEqual(
            (product_event,),
            self.repository.list_product_events(conversation.id),
        )
        self.assertEqual((), self.repository.list_product_events(conversation.id, after_sequence=1))
        self.assertEqual(
            {source_event.event_id},
            self.repository.list_product_event_source_ids(conversation.id),
        )

    def test_lane_entry_run_turn_tool_and_event_round_trip(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.MAIN,
        )
        user_entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
            display={"role": "user"},
        )
        assistant_entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "正在读取"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )

        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=user_entry.id,
        )
        turn = self.repository.create_model_turn(
            run_id=run.id,
            provider="fake",
            model="fake-model",
        )
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
        )
        first_event = self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={"triggerEntryId": user_entry.id},
        )
        second_event = self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_started",
            payload={"toolExecutionId": tool.id},
        )
        pointer = self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )

        reopened = SqliteRuntimeV2Repository(self.database)
        self.assertEqual(lane.id, reopened.get_lane(lane.id).id)
        self.assertEqual(
            (user_entry.id, assistant_entry.id),
            tuple(entry.id for entry in reopened.list_entries(lane.id)),
        )
        self.assertEqual(user_entry.id, reopened.get_entry(user_entry.id).id)
        self.assertEqual(run.id, reopened.get_run(run.id).id)
        self.assertEqual(turn.id, reopened.get_model_turn(turn.id).id)
        self.assertEqual(tool.id, reopened.get_tool_execution(tool.id).id)
        self.assertEqual(
            (first_event.event_id, second_event.event_id),
            tuple(event.event_id for event in reopened.list_runtime_events(run.id)),
        )
        self.assertEqual(
            second_event.event_id,
            reopened.list_runtime_events(run.id, after_sequence=1)[0].event_id,
        )
        self.assertEqual(pointer.active_run_id, reopened.get_conversation_pointer(conversation.id).active_run_id)

    def test_branch_lane_reuses_base_entry_without_copying_history(self) -> None:
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        main_next = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "主线回复"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        branch = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        branch_entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=branch.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "分支消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )

        self.assertEqual(base.id, branch.base_entry_id)
        self.assertEqual(base.id, branch.leaf_entry_id)
        self.assertEqual((base.id, main_next.id), tuple(
            entry.id for entry in self.repository.list_entries(main.id)
        ))
        self.assertEqual((branch_entry.id,), tuple(
            entry.id for entry in self.repository.list_entries(branch.id)
        ))
        self.assertEqual((base.id, branch_entry.id), tuple(
            entry.id for entry in self.repository.list_lane_context_entries(branch.id)
        ))

    def test_promote_lane_swaps_main_role_without_moving_entries(self) -> None:
        conversation = self.chat_repository.create_conversation()
        main = self.repository.create_lane(conversation_id=conversation.id)
        base = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        branch = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        branch_entry = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=branch.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "分支消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        event = self.repository.append_lane_event(
            conversation_id=conversation.id,
            lane_id=branch.id,
            event_type="branch.created",
            data={"sourceLaneId": main.id},
        )

        result = self.repository.promote_lane(
            conversation_id=conversation.id,
            target_lane_id=branch.id,
        )

        self.assertEqual(branch.id, result.promoted_lane.id)
        self.assertEqual(LaneKind.MAIN, result.promoted_lane.kind)
        self.assertEqual(LaneStatus.ACTIVE, result.promoted_lane.status)
        self.assertEqual(main.id, result.previous_main_lane.id)
        self.assertEqual(LaneKind.PERSISTENT_BRANCH, result.previous_main_lane.kind)
        self.assertEqual(branch.id, result.pointer.active_lane_id)
        self.assertEqual(
            (base.id, branch_entry.id),
            tuple(
                entry.id
                for entry in self.repository.list_lane_context_entries(branch.id)
            ),
        )
        self.assertEqual(
            (event.event_id,),
            tuple(
                lane_event.event_id
                for lane_event in self.repository.list_conversation_lane_events(
                    conversation.id
                )
            ),
        )

    def test_entry_parent_must_match_lane_leaf(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        first = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "first"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        second = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "second"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )

        with self.assertRaises(ConflictError):
            self.repository.append_entry(
                conversation_id=conversation.id,
                lane_id=lane.id,
                type=TranscriptEntryType.ASSISTANT_MESSAGE,
                actor=Actor.ASSISTANT,
                payload={"content": "invalid parent"},
                context_policy={"include_in_llm": True, "transform": "full"},
                parent_id=first.id,
            )
        self.assertEqual(second.id, self.repository.get_lane(lane.id).leaf_entry_id)

    def test_only_one_active_run_per_lane(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "run"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )

        with self.assertRaises(ConflictError):
            self.repository.create_run(
                conversation_id=conversation.id,
                lane_id=lane.id,
                trigger_entry_id=trigger.id,
            )

    def test_active_run_variant_controls_entry_projection(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "重新生成"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        first_run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
            is_active_variant=True,
        )
        first_assistant = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "第一版"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=trigger.id,
            source_run_id=first_run.id,
        )
        self.repository.set_run_assistant_entry(
            run_id=first_run.id,
            assistant_entry_id=first_assistant.id,
            is_active_variant=True,
        )
        self.repository.update_run_status(first_run.id, RunStatus.COMPLETED)

        second_run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
            sibling_group_id=first_run.sibling_group_id,
        )
        second_assistant = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "第二版"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=trigger.id,
            source_run_id=second_run.id,
            allow_variant_sibling=True,
        )
        self.repository.set_run_assistant_entry(
            run_id=second_run.id,
            assistant_entry_id=second_assistant.id,
        )

        self.assertEqual(
            (trigger.id, first_assistant.id),
            tuple(entry.id for entry in self.repository.list_entries(lane.id)),
        )
        self.assertEqual(
            (trigger.id, first_assistant.id, second_assistant.id),
            tuple(
                entry.id
                for entry in self.repository.list_entries(lane.id, include_variants=True)
            ),
        )

        self.repository.set_active_run_variant(second_run.id)
        self.assertEqual(
            (trigger.id, second_assistant.id),
            tuple(entry.id for entry in self.repository.list_entries(lane.id)),
        )

    def test_terminal_states_are_immutable(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "run"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        turn = self.repository.create_model_turn(run_id=run.id)
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_1",
            tool_name="read_file",
            arguments={},
        )
        self.repository.update_tool_execution_status(
            tool.id,
            ToolExecutionStatus.COMPLETED,
        )
        with self.assertRaises(InvalidStateError):
            self.repository.update_tool_execution_status(
                tool.id,
                ToolExecutionStatus.RUNNING,
            )

        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.COMPLETED,
            input_tokens=10,
            output_tokens=5,
        )
        with self.assertRaises(InvalidStateError):
            self.repository.update_model_turn_status(
                turn.id,
                ModelTurnStatus.STREAMING,
            )

        self.repository.update_run_status(run.id, RunStatus.COMPLETED)
        with self.assertRaises(InvalidStateError):
            self.repository.update_run_status(run.id, RunStatus.RUNNING)
        with self.assertRaises(InvalidStateError):
            self.repository.create_model_turn(run_id=run.id)

        other_conversation = self.chat_repository.create_conversation()
        active_turn = self.repository.create_model_turn(
            run_id=self._create_active_run(other_conversation.id)
        )
        self.repository.update_model_turn_status(
            active_turn.id,
            ModelTurnStatus.COMPLETED,
        )
        with self.assertRaises(InvalidStateError):
            self.repository.create_tool_execution(
                model_turn_id=active_turn.id,
                call_id="call_2",
                tool_name="read_file",
                arguments={},
            )

    def _create_active_run(self, conversation_id: str) -> str:
        lane = self.repository.create_lane(conversation_id=conversation_id)
        trigger = self.repository.append_entry(
            conversation_id=conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "another run"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=conversation_id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        ).id

    def test_streaming_entry_cannot_have_child(self) -> None:
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "partial"},
            context_policy={"include_in_llm": True, "transform": "full"},
            status=TranscriptEntryStatus.STREAMING,
        )

        with self.assertRaises(InvalidStateError):
            self.repository.append_entry(
                conversation_id=conversation.id,
                lane_id=lane.id,
                type=TranscriptEntryType.ASSISTANT_MESSAGE,
                actor=Actor.ASSISTANT,
                payload={"content": "next"},
                context_policy={"include_in_llm": True, "transform": "full"},
            )
