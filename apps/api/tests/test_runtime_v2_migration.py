from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ConversationKind
from endless_task.runtime_v2 import RuntimeV2MigrationService
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
        return f"2026-08-28T01:00:{self._value:02d}.000Z"


class RuntimeV2MigrationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "migration.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(
            self.database,
            clock=SequenceClock(),
            id_factory=SequenceIdFactory(),
        )
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _simulate_legacy_v10_temporary_lane(
        self,
        *,
        parent_conversation_id: str,
        temporary_conversation_id: str,
        source_leaf_entry_id: str,
    ) -> None:
        lane_id = f"v2_lane_{temporary_conversation_id}"
        parent_lane_id = f"v2_lane_{parent_conversation_id}"
        snapshot_prefix = f"v2_entry_snapshot_{temporary_conversation_id}_%"
        legacy_metadata = {
            "sourceConversationId": temporary_conversation_id,
            "treeConversationId": parent_conversation_id,
            "kind": "temporary",
            "legacyConversationKind": "ephemeral",
            "v1ReadOnly": True,
            "migratedFrom": "v1_conversation",
            "sourceParentConversationId": parent_conversation_id,
            "sourceLaneId": parent_lane_id,
            "baseEntryId": source_leaf_entry_id,
        }
        with self.database.transaction() as connection:
            snapshot_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM v2_transcript_entries
                    WHERE lane_id = ? AND id LIKE ?
                    """,
                    (lane_id, snapshot_prefix),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE v2_transcript_entries
                SET parent_id = ?
                WHERE lane_id = ?
                  AND parent_id IN (
                      SELECT id FROM v2_transcript_entries
                      WHERE lane_id = ? AND id LIKE ?
                  )
                """,
                (source_leaf_entry_id, lane_id, lane_id, snapshot_prefix),
            )
            connection.execute(
                """
                DELETE FROM v2_transcript_entries
                WHERE lane_id = ? AND id LIKE ?
                """,
                (lane_id, snapshot_prefix),
            )
            if snapshot_count:
                connection.execute(
                    """
                    UPDATE v2_transcript_entries
                    SET seq = seq + 1000000
                    WHERE lane_id = ?
                    """,
                    (lane_id,),
                )
                connection.execute(
                    """
                    UPDATE v2_transcript_entries
                    SET seq = seq - 1000000 - ?
                    WHERE lane_id = ? AND seq >= 1000000
                    """,
                    (snapshot_count, lane_id),
                )
            connection.execute(
                """
                UPDATE v2_transcript_entries
                SET parent_id = ?
                WHERE lane_id = ?
                  AND parent_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM v2_transcript_entries AS parent
                      WHERE parent.id = v2_transcript_entries.parent_id
                        AND parent.lane_id = ?
                  )
                """,
                (source_leaf_entry_id, lane_id, lane_id),
            )
            connection.execute(
                """
                UPDATE v2_transcript_entries
                SET conversation_id = ?
                WHERE lane_id = ?
                """,
                (parent_conversation_id, lane_id),
            )
            connection.execute(
                "UPDATE v2_runs SET conversation_id = ? WHERE lane_id = ?",
                (parent_conversation_id, lane_id),
            )
            connection.execute(
                """
                UPDATE v2_context_compactions
                SET conversation_id = ?, base_entry_id = ?
                WHERE lane_id = ?
                """,
                (parent_conversation_id, source_leaf_entry_id, lane_id),
            )
            leaf_row = connection.execute(
                """
                SELECT id FROM v2_transcript_entries
                WHERE lane_id = ?
                ORDER BY seq DESC, id DESC
                LIMIT 1
                """,
                (lane_id,),
            ).fetchone()
            legacy_leaf_entry_id = (
                leaf_row["id"] if leaf_row is not None else source_leaf_entry_id
            )
            connection.execute(
                """
                UPDATE v2_lanes
                SET conversation_id = ?, kind = 'temporary',
                    base_entry_id = ?, leaf_entry_id = ?,
                    metadata_json = ?, source_lane_id = ?,
                    created_from_entry_id = ?
                WHERE id = ?
                """,
                (
                    parent_conversation_id,
                    source_leaf_entry_id,
                    legacy_leaf_entry_id,
                    json.dumps(
                        legacy_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    parent_lane_id,
                    source_leaf_entry_id,
                    lane_id,
                ),
            )
            connection.execute(
                "DELETE FROM v2_temporary_conversations WHERE conversation_id = ?",
                (temporary_conversation_id,),
            )
            connection.execute(
                "DELETE FROM v2_conversation_pointers WHERE conversation_id = ?",
                (temporary_conversation_id,),
            )
            connection.execute(
                """
                UPDATE v2_conversation_pointers
                SET active_lane_id = ?, active_run_id = NULL,
                    active_run_variant_id = NULL
                WHERE conversation_id = ?
                """,
                (lane_id, parent_conversation_id),
            )
            parent_event_seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(event_seq), 0) + 1
                    FROM v2_lane_events
                    WHERE conversation_id = ?
                    """,
                    (parent_conversation_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE v2_lane_events
                SET conversation_id = ?, event_seq = ?,
                    event_type = 'branch.created', data_json = ?
                WHERE id = ?
                """,
                (
                    parent_conversation_id,
                    parent_event_seq,
                    json.dumps(
                        legacy_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"v2_lane_event_{temporary_conversation_id}",
                ),
            )
            connection.execute(
                """
                UPDATE v2_migration_conversation_mappings
                SET tree_conversation_id = ?, lane_kind = 'temporary'
                WHERE source_conversation_id = ?
                """,
                (parent_conversation_id, temporary_conversation_id),
            )

    def test_migrates_chat_runtime_tool_event_summary_and_run_variants(self) -> None:
        conversation = self.chat_repository.create_conversation()
        turn = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="读取文件",
        )
        first_variant = turn.response_variants[0]

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET content = '这是第一版回答'
                WHERE id = ?
                """,
                (first_variant.assistant_message.id,),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, turn_id, role, content, created_at, updated_at
                )
                VALUES (?, ?, ?, 'assistant', '这是第二版回答', ?, ?)
                """,
                (
                    "message_regen",
                    conversation.id,
                    turn.turn.id,
                    "2026-08-28T01:00:10.000Z",
                    "2026-08-28T01:00:11.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO response_variants(
                    id, turn_id, assistant_message_id, variant_index,
                    operation, status, provider, model, finish_reason,
                    input_tokens, output_tokens, created_at, started_at, finished_at
                )
                VALUES (?, ?, ?, 2, 'regenerate', 'completed', 'fake',
                        'fake-model', 'stop', 20, 15, ?, ?, ?)
                """,
                (
                    "variant_regen",
                    turn.turn.id,
                    "message_regen",
                    "2026-08-28T01:00:12.000Z",
                    "2026-08-28T01:00:13.000Z",
                    "2026-08-28T01:00:14.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO tool_calls(
                    turn_id, id, conversation_id, response_variant_id,
                    tool_name, arguments_json, effect, approval_mode,
                    status, result_truncated, created_at, started_at, finished_at
                )
                VALUES (?, 'tool_1', ?, ?, 'read_file', ?, 'read_only',
                        'auto', 'completed', 0, ?, ?, ?)
                """,
                (
                    turn.turn.id,
                    conversation.id,
                    first_variant.variant.id,
                    json.dumps({"path": "a.txt"}),
                    "2026-08-28T01:00:15.000Z",
                    "2026-08-28T01:00:16.000Z",
                    "2026-08-28T01:00:17.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_events(
                    event_id, turn_id, sequence, event_type, payload_json,
                    occurred_at, response_variant_id, message_id
                )
                VALUES (?, ?, 1, 'message.delta', ?, ?, ?, ?)
                """,
                (
                    "event_1",
                    turn.turn.id,
                    json.dumps(
                        {
                            "version": 1,
                            "eventId": "event_1",
                            "sequence": 1,
                            "type": "message.delta",
                            "conversationId": conversation.id,
                            "turnId": turn.turn.id,
                            "occurredAt": "2026-08-28T01:00:18.000Z",
                            "data": {"delta": "第一"},
                            "responseVariantId": first_variant.variant.id,
                            "messageId": first_variant.assistant_message.id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "2026-08-28T01:00:18.000Z",
                    first_variant.variant.id,
                    first_variant.assistant_message.id,
                ),
            )
            connection.execute(
                """
                INSERT INTO conversation_summary_revisions(
                    id, conversation_id, through_turn_ordinal,
                    prompt_version, content, input_token_estimate, created_at
                )
                VALUES (?, ?, 1, 'p0-v1', '会话摘要', 30, ?)
                """,
                (
                    "summary_1",
                    conversation.id,
                    "2026-08-28T01:00:19.000Z",
                ),
            )
            connection.execute(
                """
                UPDATE turns
                SET status = 'completed',
                    active_response_variant_id = ?,
                    started_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    first_variant.variant.id,
                    "2026-08-28T01:00:08.000Z",
                    "2026-08-28T01:00:20.000Z",
                    turn.turn.id,
                ),
            )
            connection.execute(
                """
                UPDATE response_variants
                SET status = 'completed', finish_reason = 'stop',
                    input_tokens = 10, output_tokens = 8,
                    started_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    "2026-08-28T01:00:09.000Z",
                    "2026-08-28T01:00:20.000Z",
                    first_variant.variant.id,
                ),
            )

        with self.database.connect() as connection:
            v1_counts_before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "conversations",
                    "messages",
                    "turns",
                    "response_variants",
                    "tool_calls",
                    "runtime_events",
                    "conversation_summary_revisions",
                )
            }

        report = RuntimeV2MigrationService(self.database).migrate()

        self.assertFalse(report.already_migrated)
        self.assertEqual(1, report.conversation_count)
        self.assertEqual(1, report.lane_count)
        self.assertEqual(6, report.entry_count)
        self.assertEqual(2, report.run_count)
        self.assertEqual(2, report.model_turn_count)
        self.assertEqual(1, report.tool_execution_count)
        self.assertEqual(1, report.runtime_event_count)
        self.assertEqual(1, report.context_compaction_count)
        self.assertTrue(report.warnings)

        lane_id = f"v2_lane_{conversation.id}"
        active_entries = self.repository.list_entries(lane_id)
        all_entries = self.repository.list_entries(lane_id, include_variants=True)
        self.assertEqual(
            (
                f"v2_entry_{turn.user_message.id}",
                f"v2_entry_{first_variant.assistant_message.id}",
                f"v2_entry_tool_call_{turn.turn.id}_tool_1",
                f"v2_entry_tool_result_{turn.turn.id}_tool_1",
                "v2_entry_summary_summary_1",
            ),
            tuple(entry.id for entry in active_entries),
        )
        self.assertEqual(6, len(all_entries))

        first_run = self.repository.get_run(f"v2_run_{first_variant.variant.id}")
        second_run = self.repository.get_run("v2_run_variant_regen")
        self.assertTrue(first_run.is_active_variant)
        self.assertFalse(second_run.is_active_variant)
        self.assertEqual(
            f"v2_entry_{first_variant.assistant_message.id}",
            first_run.assistant_entry_id,
        )
        self.assertEqual(
            f"v2_entry_message_regen",
            second_run.assistant_entry_id,
        )

        events = self.repository.list_runtime_events(first_run.id)
        self.assertEqual(1, len(events))
        self.assertEqual("v2_event_event_1", events[0].event_id)
        self.assertEqual("message.delta", events[0].event_type)
        self.assertEqual("第一", events[0].payload["delta"])
        self.assertEqual("event_1", events[0].payload["sourceEventId"])

        compactions = self.repository.list_context_compactions(lane_id)
        self.assertEqual(1, len(compactions))
        self.assertEqual("v2_entry_summary_summary_1", compactions[0].summary_entry_id)
        self.assertEqual(30, compactions[0].tokens_before)

        pointer = self.repository.get_conversation_pointer(conversation.id)
        self.assertIsNotNone(pointer)
        self.assertEqual(lane_id, pointer.active_lane_id)
        self.assertEqual(first_run.id, pointer.active_run_id)

        with self.database.connect() as connection:
            v1_counts_after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "conversations",
                    "messages",
                    "turns",
                    "response_variants",
                    "tool_calls",
                    "runtime_events",
                    "conversation_summary_revisions",
                )
            }
        self.assertEqual(v1_counts_before, v1_counts_after)

    def test_migration_is_idempotent(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="你好",
        )
        service = RuntimeV2MigrationService(self.database)
        first = service.migrate()
        second = service.migrate()

        self.assertFalse(first.already_migrated)
        self.assertTrue(second.already_migrated)
        self.assertEqual(first.conversation_count, second.conversation_count)
        self.assertEqual(first.entry_count, second.entry_count)
        self.assertEqual(first.run_count, second.run_count)

        with self.database.connect() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM v2_migration_state"
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM v2_transcript_entries"
                ).fetchone()[0],
            )

    def test_v1_persistent_branch_family_migrates_into_one_conversation_tree(self) -> None:
        parent = self.chat_repository.create_conversation()
        parent_turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="parent-request",
            content="主会话输入",
        )
        branch = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=parent_turn.turn.id,
            kind=ConversationKind.NORMAL,
        )
        branch_turn = self.chat_repository.create_turn(
            conversation_id=branch.id,
            client_request_id="branch-request",
            content="持久分支输入",
        )

        report = RuntimeV2MigrationService(self.database).migrate()

        self.assertEqual(2, report.conversation_count)
        self.assertEqual(2, report.lane_count)
        self.assertEqual(1, report.conversation_tree_count)
        self.assertEqual(1, report.branch_conversation_count)
        self.assertEqual(2, report.conversation_mapping_count)

        lanes = self.repository.list_lanes(parent.id)
        self.assertEqual(
            ("main", "persistent_branch"),
            tuple(lane.kind.value for lane in lanes),
        )
        self.assertEqual(
            (parent.id, parent.id),
            tuple(lane.conversation_id for lane in lanes),
        )

        branch_lane = self.repository.get_lane(f"v2_lane_{branch.id}")
        self.assertEqual(
            f"v2_entry_{parent_turn.response_variants[0].assistant_message.id}",
            branch_lane.base_entry_id,
        )
        self.assertEqual(f"v2_lane_{parent.id}", branch_lane.metadata["sourceLaneId"])
        self.assertEqual(branch.id, branch_lane.metadata["sourceConversationId"])
        self.assertTrue(branch_lane.metadata["v1ReadOnly"])

        branch_context = self.repository.list_lane_context_entries(branch_lane.id)
        self.assertIn(
            f"v2_entry_{parent_turn.user_message.id}",
            tuple(entry.id for entry in branch_context),
        )
        self.assertIn(
            f"v2_entry_{branch_turn.user_message.id}",
            tuple(entry.id for entry in branch_context),
        )
        self.assertTrue(
            all(entry.conversation_id == parent.id for entry in branch_context)
        )

        pointer = self.repository.get_conversation_pointer(parent.id)
        self.assertEqual(f"v2_lane_{parent.id}", pointer.active_lane_id)
        with self.database.connect() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM v2_conversation_pointers"
                ).fetchone()[0],
            )
            mappings = connection.execute(
                """
                SELECT source_conversation_id, tree_conversation_id, lane_id,
                       lane_kind, v1_read_only
                FROM v2_migration_conversation_mappings
                ORDER BY source_conversation_id
                """
            ).fetchall()
        self.assertEqual(
            (
                (parent.id, parent.id, f"v2_lane_{parent.id}", "main", 1),
                (branch.id, parent.id, f"v2_lane_{branch.id}", "persistent_branch", 1),
            ),
            tuple(
                (
                    row["source_conversation_id"],
                    row["tree_conversation_id"],
                    row["lane_id"],
                    row["lane_kind"],
                    row["v1_read_only"],
                )
                for row in mappings
            ),
        )

    def test_legacy_ephemeral_conversations_migrate_as_independent_temporary_conversations(self) -> None:
        parent = self.chat_repository.create_conversation()
        parent_turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="parent-request",
            content="主会话输入",
        )
        temporary = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=parent_turn.turn.id,
        )
        temporary_turn = self.chat_repository.create_turn(
            conversation_id=temporary.id,
            client_request_id="temporary-request",
            content="临时探索输入",
        )
        nested_temporary = self.chat_repository.create_branch(
            parent_conversation_id=temporary.id,
            fork_turn_id=temporary_turn.turn.id,
        )
        self.chat_repository.create_turn(
            conversation_id=nested_temporary.id,
            client_request_id="nested-request",
            content="嵌套临时探索输入",
        )

        report = RuntimeV2MigrationService(self.database).migrate()

        self.assertEqual(3, report.conversation_count)
        self.assertEqual(3, report.lane_count)
        self.assertEqual(3, report.conversation_tree_count)
        self.assertEqual(0, report.branch_conversation_count)
        self.assertEqual(3, report.conversation_mapping_count)

        self.assertEqual(
            ("main",),
            tuple(lane.kind.value for lane in self.repository.list_lanes(parent.id)),
        )
        temporary_lane = self.repository.get_lane(f"v2_lane_{temporary.id}")
        self.assertEqual("temporary", temporary_lane.kind.value)
        self.assertEqual(temporary.id, temporary_lane.conversation_id)
        self.assertIsNone(temporary_lane.base_entry_id)
        self.assertEqual(f"v2_lane_{parent.id}", temporary_lane.metadata["sourceLaneId"])
        self.assertEqual(
            f"v2_entry_{parent_turn.response_variants[0].assistant_message.id}",
            temporary_lane.metadata["sourceLeafEntryId"],
        )
        self.assertEqual(
            "legacy_temporary_conversation",
            temporary_lane.metadata["independentTreeReason"],
        )

        temporary_context = self.repository.list_lane_context_entries(temporary_lane.id)
        temporary_user_contents = tuple(
            entry.payload.get("content")
            for entry in temporary_context
            if entry.actor.value == "user"
        )
        self.assertEqual(("主会话输入", "临时探索输入"), temporary_user_contents)
        self.assertTrue(
            all(entry.conversation_id == temporary.id for entry in temporary_context)
        )
        self.assertTrue(
            temporary_context[0].display.get("sourceEntryId", "").startswith("v2_entry_")
        )

        nested_lane = self.repository.get_lane(f"v2_lane_{nested_temporary.id}")
        self.assertEqual("temporary", nested_lane.kind.value)
        self.assertEqual(nested_temporary.id, nested_lane.conversation_id)
        self.assertEqual(temporary_lane.id, nested_lane.metadata["sourceLaneId"])
        self.assertEqual(
            f"v2_entry_{temporary_turn.response_variants[0].assistant_message.id}",
            nested_lane.metadata["sourceLeafEntryId"],
        )

        pointer = self.repository.get_conversation_pointer(parent.id)
        self.assertEqual(f"v2_lane_{parent.id}", pointer.active_lane_id)
        self.assertEqual(
            f"v2_lane_{temporary.id}",
            self.repository.get_conversation_pointer(temporary.id).active_lane_id,
        )
        self.assertEqual(
            f"v2_lane_{nested_temporary.id}",
            self.repository.get_conversation_pointer(nested_temporary.id).active_lane_id,
        )
        with self.database.connect() as connection:
            self.assertEqual(
                3,
                connection.execute(
                    "SELECT COUNT(*) FROM v2_conversation_pointers"
                ).fetchone()[0],
            )
            temporary_records = connection.execute(
                """
                SELECT conversation_id, source_conversation_id, source_lane_id,
                       source_leaf_entry_id, snapshot_entry_ids_json, promoted_at
                FROM v2_temporary_conversations
                ORDER BY conversation_id
                """
            ).fetchall()
            mappings = connection.execute(
                """
                SELECT source_conversation_id, tree_conversation_id, lane_id,
                       lane_kind, v1_read_only
                FROM v2_migration_conversation_mappings
                ORDER BY source_conversation_id
                """
            ).fetchall()
        self.assertEqual(2, len(temporary_records))
        by_conversation = {row["conversation_id"]: row for row in temporary_records}
        self.assertEqual(parent.id, by_conversation[temporary.id]["source_conversation_id"])
        self.assertEqual(
            f"v2_lane_{parent.id}",
            by_conversation[temporary.id]["source_lane_id"],
        )
        self.assertIsNone(by_conversation[temporary.id]["promoted_at"])
        snapshot = json.loads(by_conversation[temporary.id]["snapshot_entry_ids_json"])
        self.assertEqual(2, len(snapshot["entryIds"]))
        self.assertEqual(2, len(snapshot["copiedEntryIds"]))
        self.assertEqual(
            (
                (parent.id, parent.id, f"v2_lane_{parent.id}", "main", 1),
                (temporary.id, temporary.id, f"v2_lane_{temporary.id}", "temporary", 1),
                (
                    nested_temporary.id,
                    nested_temporary.id,
                    f"v2_lane_{nested_temporary.id}",
                    "temporary",
                    1,
                ),
            ),
            tuple(
                (
                    row["source_conversation_id"],
                    row["tree_conversation_id"],
                    row["lane_id"],
                    row["lane_kind"],
                    row["v1_read_only"],
                )
                for row in mappings
            ),
        )

    def test_promoted_v1_ephemeral_becomes_independent_formal_conversation(self) -> None:
        parent = self.chat_repository.create_conversation()
        parent_turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="parent-request",
            content="主会话输入",
        )
        temporary = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=parent_turn.turn.id,
        )
        temporary_turn = self.chat_repository.create_turn(
            conversation_id=temporary.id,
            client_request_id="temporary-request",
            content="升级后的临时对话输入",
        )
        self.chat_repository.promote_conversation(temporary.id)

        report = RuntimeV2MigrationService(self.database).migrate()

        self.assertEqual(2, report.conversation_tree_count)
        self.assertEqual(0, report.branch_conversation_count)
        parent_lanes = {
            lane.id: lane.kind.value
            for lane in self.repository.list_lanes(parent.id)
        }
        promoted_lanes = {
            lane.id: lane.kind.value
            for lane in self.repository.list_lanes(temporary.id)
        }
        self.assertEqual({f"v2_lane_{parent.id}": "main"}, parent_lanes)
        self.assertEqual({f"v2_lane_{temporary.id}": "main"}, promoted_lanes)

        parent_pointer = self.repository.get_conversation_pointer(parent.id)
        promoted_pointer = self.repository.get_conversation_pointer(temporary.id)
        self.assertEqual(f"v2_lane_{parent.id}", parent_pointer.active_lane_id)
        self.assertEqual(f"v2_lane_{temporary.id}", promoted_pointer.active_lane_id)
        self.assertEqual(
            f"v2_run_{temporary_turn.response_variants[0].variant.id}",
            promoted_pointer.active_run_id,
        )

        promoted_lane = self.repository.get_lane(f"v2_lane_{temporary.id}")
        self.assertEqual(f"v2_lane_{parent.id}", promoted_lane.metadata["sourceLaneId"])
        self.assertEqual(
            f"v2_entry_{parent_turn.response_variants[0].assistant_message.id}",
            promoted_lane.metadata["sourceLeafEntryId"],
        )
        self.assertEqual(
            "legacy_promoted_conversation",
            promoted_lane.metadata["independentTreeReason"],
        )
        promoted_context = self.repository.list_lane_context_entries(promoted_lane.id)
        self.assertEqual(
            ("主会话输入", "升级后的临时对话输入"),
            tuple(
                entry.payload.get("content")
                for entry in promoted_context
                if entry.actor.value == "user"
            ),
        )
        self.assertTrue(
            all(entry.conversation_id == temporary.id for entry in promoted_context)
        )

        self.assertEqual((), self.repository.list_conversation_lane_events(parent.id))
        with self.database.connect() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM v2_temporary_conversations"
                ).fetchone()[0],
            )
            mappings = connection.execute(
                """
                SELECT source_conversation_id, tree_conversation_id, lane_id,
                       lane_kind, v1_read_only
                FROM v2_migration_conversation_mappings
                ORDER BY source_conversation_id
                """
            ).fetchall()
        self.assertEqual(
            (
                (parent.id, parent.id, f"v2_lane_{parent.id}", "main", 1),
                (temporary.id, temporary.id, f"v2_lane_{temporary.id}", "main", 1),
            ),
            tuple(
                (
                    row["source_conversation_id"],
                    row["tree_conversation_id"],
                    row["lane_id"],
                    row["lane_kind"],
                    row["v1_read_only"],
                )
                for row in mappings
            ),
        )

    def test_existing_v10_temporary_lane_is_repaired_idempotently(self) -> None:
        parent = self.chat_repository.create_conversation()
        parent_turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="parent-request",
            content="主会话输入",
        )
        temporary = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=parent_turn.turn.id,
        )
        temporary_turn = self.chat_repository.create_turn(
            conversation_id=temporary.id,
            client_request_id="temporary-request",
            content="旧临时 lane 输入",
        )
        service = RuntimeV2MigrationService(self.database)
        service.migrate()
        source_leaf_entry_id = (
            f"v2_entry_{parent_turn.response_variants[0].assistant_message.id}"
        )
        self._simulate_legacy_v10_temporary_lane(
            parent_conversation_id=parent.id,
            temporary_conversation_id=temporary.id,
            source_leaf_entry_id=source_leaf_entry_id,
        )

        pending_audit = service.audit()
        self.assertFalse(pending_audit.passed)
        self.assertEqual(1, pending_audit.pending_legacy_temporary_lane_repair_count)
        self.assertIn(
            "legacy_temporary_lane_repair_pending_count=1",
            pending_audit.errors,
        )

        repaired = service.migrate()
        repeated = service.migrate()

        self.assertTrue(repaired.already_migrated)
        self.assertEqual(1, repaired.legacy_temporary_lane_repair_count)
        self.assertEqual(1, repeated.legacy_temporary_lane_repair_count)
        self.assertEqual(repaired.entry_count, repeated.entry_count)

        parent_pointer = self.repository.get_conversation_pointer(parent.id)
        temporary_pointer = self.repository.get_conversation_pointer(temporary.id)
        self.assertEqual(f"v2_lane_{parent.id}", parent_pointer.active_lane_id)
        self.assertEqual(f"v2_lane_{temporary.id}", temporary_pointer.active_lane_id)
        self.assertEqual(
            f"v2_run_{temporary_turn.response_variants[0].variant.id}",
            temporary_pointer.active_run_id,
        )

        self.assertEqual(
            ("main",),
            tuple(lane.kind.value for lane in self.repository.list_lanes(parent.id)),
        )
        temporary_lane = self.repository.get_lane(f"v2_lane_{temporary.id}")
        self.assertEqual("temporary", temporary_lane.kind.value)
        self.assertEqual(temporary.id, temporary_lane.conversation_id)
        self.assertIsNone(temporary_lane.base_entry_id)
        self.assertEqual(f"v2_lane_{parent.id}", temporary_lane.source_lane_id)
        self.assertEqual(source_leaf_entry_id, temporary_lane.created_from_entry_id)
        self.assertTrue(temporary_lane.metadata["legacyTemporaryLaneRepair"])
        self.assertEqual(
            "legacy_temporary_lane_repair",
            temporary_lane.metadata["independentTreeReason"],
        )

        temporary_context = self.repository.list_lane_context_entries(temporary_lane.id)
        self.assertEqual(
            ("主会话输入", "旧临时 lane 输入"),
            tuple(
                entry.payload.get("content")
                for entry in temporary_context
                if entry.actor.value == "user"
            ),
        )
        self.assertTrue(
            all(entry.conversation_id == temporary.id for entry in temporary_context)
        )
        self.assertTrue(
            temporary_context[0].display.get("sourceEntryId", "").startswith("v2_entry_")
        )

        temporary_record = self.repository.get_temporary_conversation(temporary.id)
        self.assertEqual(parent.id, temporary_record.source_conversation_id)
        self.assertEqual(f"v2_lane_{parent.id}", temporary_record.source_lane_id)
        self.assertEqual(source_leaf_entry_id, temporary_record.source_leaf_entry_id)
        self.assertEqual(2, len(temporary_record.snapshot_entry_ids))
        events = self.repository.list_conversation_lane_events(temporary.id)
        self.assertEqual(("temporary_conversation.created",), tuple(event.event_type for event in events))
        self.assertTrue(events[0].data["legacyTemporaryLaneRepair"])

        with self.database.connect() as connection:
            mapping = connection.execute(
                """
                SELECT tree_conversation_id, lane_kind
                FROM v2_migration_conversation_mappings
                WHERE source_conversation_id = ?
                """,
                (temporary.id,),
            ).fetchone()
        self.assertEqual(temporary.id, mapping["tree_conversation_id"])
        self.assertEqual("temporary", mapping["lane_kind"])

        audit = service.audit()
        self.assertTrue(audit.passed)
        self.assertEqual(1, audit.legacy_temporary_lane_repair_count)
        self.assertEqual(0, audit.pending_legacy_temporary_lane_repair_count)

    def test_interrupted_v1_execution_is_not_resumed(self) -> None:
        conversation = self.chat_repository.create_conversation()
        turn = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="继续执行",
        )

        report = RuntimeV2MigrationService(self.database).migrate()
        run = self.repository.get_run(
            f"v2_run_{turn.response_variants[0].variant.id}"
        )

        self.assertEqual("failed", run.status.value)
        self.assertEqual("v1_interrupted", run.error_code)
        self.assertIn(
            "interrupted before migration",
            run.safe_message,
        )
        self.assertTrue(
            any("interrupted before migration" in warning for warning in report.warnings)
        )
