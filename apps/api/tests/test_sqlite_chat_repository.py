from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ConversationStatus,
    FinishReason,
    ResponseVariantOperation,
    ResponseVariantStatus,
    TurnStatus,
)
from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError
from endless_task.storage import Database, SqliteChatRepository


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
        return f"2026-08-22T00:00:{self._value:02d}.000Z"


class SqliteChatRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "endless-task.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.repository = SqliteChatRepository(
            self.database,
            clock=SequenceClock(),
            id_factory=SequenceIdFactory(),
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_migrations_are_idempotent(self) -> None:
        self.database.initialize()
        self.assertEqual(
            (
                "001_initial.sql",
                "002_runtime_event_identity.sql",
                "003_context_management.sql",
                "004_conversation_files.sql",
                "005_tool_approvals.sql",
                "006_memory.sql",
                "007_memory_proposals.sql",
                "008_memory_lifecycle.sql",
                "009_preferences.sql",
                "010_artifacts.sql",
                "011_artifact_proposals.sql",
                "012_artifact_proposal_labels.sql",
                "013_artifact_proposal_target.sql",
                "014_artifact_versions_source_index.sql",
                "015_artifact_proposal_base_version.sql",
                "016_tasks.sql",
                "017_task_proposals.sql",
                "018_task_runs.sql",
                "019_task_lifecycle.sql",
                "020_task_run_awaiting.sql",
                "021_task_notifications.sql",
                "022_task_run_reliability.sql",
                "023_task_reminders.sql",
                "024_conversation_branches.sql",
                "025_knowledge.sql",
                "026_knowledge_proposals.sql",
                "027_retrieval_events.sql",
                "028_embeddings.sql",
                "029_knowledge_chunks.sql",
                "030_knowledge_proposal_merge.sql",
                "031_workspaces.sql",
                "032_workspace_runtime.sql",
                "033_skill_overrides.sql",
                "034_mcp_servers.sql",
                "035_provider_profiles.sql",
                "036_runtime_v2.sql",
                "037_runtime_v2_product_events.sql",
                "038_runtime_v2_lane_events.sql",
                "039_runtime_v2_memory_scopes.sql",
                "040_runtime_v2_migration_mappings.sql",
                "041_runtime_v2_runtime_overrides.sql",
                "042_runtime_v2_lane_lifecycle_and_temporary_conversations.sql",
                "043_runtime_v2_message_idempotency.sql",
                "044_runtime_v2_main_lane_pointer.sql",
                "045_runtime_v2_promoted_main_lane_repair.sql",
                "046_provider_model_catalog.sql",
                "047_eval.sql",
                "048_memory_auto_fact_origin.sql",
                "049_runtime_v2_migrate_v1_memories.sql",
                "050_runtime_v2_memory_quality.sql",
                "051_runtime_v2_memory_update.sql",
                "052_runtime_v2_tool_error_contract.sql",
                "053_task_run_atomic_claim.sql",
                "054_delegation_conversation_parent.sql",
            "055_runtime_ledger_trace.sql",
                "056_runtime_ledger_cost.sql",
                "057_runtime_v2_resend_override.sql",
                "058_runtime_v2_drop_runtime_overrides.sql",
                "059_runtime_v2_drop_migration_tables.sql",
                "060_hub_events.sql",
                "061_response_feedback.sql",
                "062_memory_importance.sql",
                "063_memory_consolidation.sql",
                "064_undo_journal.sql",
                "065_memory_reflections.sql",
                "066_user_profiles.sql",
                "067_skill_usage.sql",
                "068_notification_turn.sql",
                "069_skill_pin.sql",
            ),
            self.database.applied_migrations(),
        )

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()
        self.assertEqual(69, row["count"])

    def test_create_turn_is_persisted_and_idempotent(self) -> None:
        conversation = self.repository.create_conversation()

        first = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="  第一条消息\n保留格式  ",
        )
        duplicate = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="这段重复请求内容不会覆盖原消息",
        )

        self.assertEqual(first.turn.id, duplicate.turn.id)
        self.assertEqual("  第一条消息\n保留格式  ", duplicate.user_message.content)
        self.assertEqual(TurnStatus.CREATED, first.turn.status)
        self.assertEqual(1, first.turn.ordinal)
        self.assertEqual(1, len(first.response_variants))
        self.assertEqual(
            ResponseVariantOperation.CREATE,
            first.response_variants[0].variant.operation,
        )

        reopened = SqliteChatRepository(self.database).get_conversation_snapshot(conversation.id)
        self.assertEqual(1, len(reopened.turns))
        self.assertEqual(first.turn.id, reopened.turns[0].turn.id)
        self.assertEqual("第一条消息 保留格式", reopened.conversation.title)

    def test_empty_conversation_can_be_reused_until_it_has_content(self) -> None:
        # 14 B1-iii-b: 非空判定以 v2 transcript 为真源。
        from endless_task.runtime_v2 import Actor, TranscriptEntryType
        from endless_task.storage import SqliteRuntimeV2Repository

        v2 = SqliteRuntimeV2Repository(self.database)
        first = self.repository.create_or_reuse_empty_conversation()
        duplicate = self.repository.create_or_reuse_empty_conversation()
        self.assertEqual(first.id, duplicate.id)

        lane = v2.create_lane(conversation_id=first.id)
        v2.append_entry(
            conversation_id=first.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "开始对话"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        next_empty = self.repository.create_or_reuse_empty_conversation()
        self.assertNotEqual(first.id, next_empty.id)

    def test_only_one_active_turn_is_allowed_per_conversation(self) -> None:
        conversation = self.repository.create_conversation()
        self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="第一条",
        )

        with self.assertRaises(ConflictError):
            self.repository.create_turn(
                conversation_id=conversation.id,
                client_request_id="request-2",
                content="第二条",
            )

    def test_completed_turn_allows_next_ordinal(self) -> None:
        conversation = self.repository.create_conversation()
        first = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="第一条",
            provider="test",
            model="fake",
        )
        first_variant_id = first.turn.active_response_variant_id
        self.repository.mark_response_running(
            turn_id=first.turn.id,
            variant_id=first_variant_id,
        )
        self.repository.complete_response(
            turn_id=first.turn.id,
            variant_id=first_variant_id,
            content="第一个回答",
            finish_reason=FinishReason.STOP,
            input_tokens=10,
            output_tokens=5,
        )

        second = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-2",
            content="第二条",
        )
        self.assertEqual(2, second.turn.ordinal)

    def test_retry_creates_a_new_variant_without_overwriting_partial_content(self) -> None:
        conversation = self.repository.create_conversation()
        turn = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="请回答",
        )
        first_variant_id = turn.turn.active_response_variant_id
        self.repository.mark_response_running(
            turn_id=turn.turn.id,
            variant_id=first_variant_id,
        )
        failed = self.repository.fail_response(
            turn_id=turn.turn.id,
            variant_id=first_variant_id,
            partial_content="部分回答",
            error_code="request_timeout",
        )

        retried = self.repository.create_response_variant(
            turn_id=turn.turn.id,
            command_request_id="retry-1",
            operation=ResponseVariantOperation.RETRY,
        )
        duplicate_retry = self.repository.create_response_variant(
            turn_id=turn.turn.id,
            command_request_id="retry-1",
            operation=ResponseVariantOperation.RETRY,
        )

        self.assertEqual(TurnStatus.FAILED, failed.turn.status)
        self.assertEqual(2, len(retried.turn_snapshot.response_variants))
        self.assertEqual(
            retried.turn_snapshot.turn.active_response_variant_id,
            duplicate_retry.turn_snapshot.turn.active_response_variant_id,
        )
        self.assertEqual(retried.response_variant_id, duplicate_retry.response_variant_id)
        self.assertEqual("部分回答", retried.turn_snapshot.response_variants[0].assistant_message.content)
        self.assertEqual(
            ResponseVariantStatus.FAILED,
            retried.turn_snapshot.response_variants[0].variant.status,
        )
        self.assertEqual(
            ResponseVariantStatus.CREATED,
            retried.turn_snapshot.response_variants[1].variant.status,
        )
        self.assertEqual(
            ResponseVariantOperation.RETRY,
            retried.turn_snapshot.response_variants[1].variant.operation,
        )

    def test_regenerate_and_variant_selection_are_limited_to_latest_turn(self) -> None:
        conversation = self.repository.create_conversation()
        first = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="生成回答",
        )
        first_variant_id = first.turn.active_response_variant_id
        self.repository.mark_response_running(
            turn_id=first.turn.id,
            variant_id=first_variant_id,
        )
        self.repository.complete_response(
            turn_id=first.turn.id,
            variant_id=first_variant_id,
            content="回答一",
            finish_reason=FinishReason.STOP,
        )

        regenerated = self.repository.create_response_variant(
            turn_id=first.turn.id,
            command_request_id="regenerate-1",
            operation=ResponseVariantOperation.REGENERATE,
        )
        second_variant_id = regenerated.response_variant_id
        self.repository.mark_response_running(
            turn_id=first.turn.id,
            variant_id=second_variant_id,
        )
        completed = self.repository.complete_response(
            turn_id=first.turn.id,
            variant_id=second_variant_id,
            content="回答二",
            finish_reason=FinishReason.STOP,
        )
        selected = self.repository.select_response_variant(
            turn_id=first.turn.id,
            variant_id=first_variant_id,
        )

        self.assertEqual(2, len(completed.response_variants))
        self.assertEqual(first_variant_id, selected.turn.active_response_variant_id)
        self.assertEqual("回答一", selected.response_variants[0].assistant_message.content)
        self.assertEqual("回答二", selected.response_variants[1].assistant_message.content)

        second_turn = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-2",
            content="继续",
        )
        self.assertEqual(2, second_turn.turn.ordinal)
        with self.assertRaises(InvalidStateError):
            self.repository.select_response_variant(
                turn_id=first.turn.id,
                variant_id=second_variant_id,
            )

    def test_archive_requires_no_active_run_and_delete_cascades(self) -> None:
        # 14 B1-iii-b: 活跃判定以 v2 run 为真源。
        from endless_task.runtime_v2 import Actor, TranscriptEntryType, RunStatus
        from endless_task.storage import SqliteRuntimeV2Repository

        v2 = SqliteRuntimeV2Repository(self.database)
        conversation = self.repository.create_conversation()
        lane = v2.create_lane(conversation_id=conversation.id)
        trigger = v2.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "临时消息"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = v2.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )

        with self.assertRaises(InvalidStateError):
            self.repository.set_conversation_status(
                conversation.id,
                ConversationStatus.ARCHIVED,
            )

        v2.update_run_status(run.id, RunStatus.CANCELLED)
        archived = self.repository.set_conversation_status(
            conversation.id,
            ConversationStatus.ARCHIVED,
        )
        self.assertEqual(ConversationStatus.ARCHIVED, archived.status)
        self.repository.delete_conversation(conversation.id)

        with self.assertRaises(NotFoundError):
            self.repository.get_conversation(conversation.id)

        with self.database.connect() as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                    "count"
                ]
                for table in (
                    "v2_runs",
                    "v2_transcript_entries",
                )
            }
        self.assertEqual({"v2_runs": 0, "v2_transcript_entries": 0}, counts)

    def test_database_rejects_two_active_turns_even_outside_repository(self) -> None:
        conversation = self.repository.create_conversation()
        first = self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="第一条",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO turns(
                        id, conversation_id, ordinal, user_message_id,
                        active_response_variant_id, status, created_at
                    ) VALUES ('turn_invalid', ?, 2, 'missing_message', NULL, 'created', ?)
                    """,
                    (conversation.id, first.turn.created_at),
                )


if __name__ == "__main__":
    unittest.main()
