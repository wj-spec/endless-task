from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ConversationKind,
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
                "070_skill_usage_search_hit.sql",
                "071_skill_provenance.sql",
                "072_delegation_child_runs.sql",
                "073_delegation_child_reporting.sql",
                "074_internal_execution_conversations.sql",
                "075_internal_execution_conversation_unique.sql",
                "076_mcp_server_tool_allowlist.sql",
            ),
            self.database.applied_migrations(),
        )

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()
        self.assertEqual(76, row["count"])

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

    def test_delegation_child_conversation_is_never_reused_as_empty(self) -> None:
        """批次 B Task 1 复审 M-7：**委派子会话**同样不许被当"新对话"交给用户。

        `create_delegation_conversation` 造的行是 `kind='normal'` + 创建时零 entry +
        `internal_owner_id` 为 NULL —— 只加 `internal_owner_id IS NULL` 挡不住它。
        一旦委派子运行在提交首条消息前失败，`POST /conversations` 就会把这条**产品
        不可见**的会话交给用户（而 `list_conversations` 又把它过滤掉 → 用户拿到一个
        左栏看不见的"当前会话"）。删掉 `AND c.delegation_parent_run_id IS NULL` 即红。
        """
        normal = self.repository.create_or_reuse_empty_conversation()
        # 委派子会话还在"零 entry"阶段（正是会被误判成空会话的窗口）。
        delegation = self.repository.create_delegation_conversation(
            "taskrun_parent_1"
        )
        self.assertIsNotNone(delegation)
        with self.database.connect() as connection:
            entries = connection.execute(
                "SELECT COUNT(*) AS n FROM v2_transcript_entries "
                "WHERE conversation_id = ?",
                (delegation.id,),
            ).fetchone()["n"]
        self.assertEqual(0, entries, "前提：委派子会话此时还没有任何 entry")

        reused = self.repository.create_or_reuse_empty_conversation()
        self.assertEqual(normal.id, reused.id)
        self.assertNotEqual(delegation.id, reused.id)

        # 而且它仍然被产品面枚举过滤（这正是"用户会拿到看不见的会话"的根因）
        listed = {item.id for item in self.repository.list_conversations()}
        self.assertNotIn(delegation.id, listed)

    def test_one_active_execution_conversation_per_owner_is_enforced(self) -> None:
        """批次 B Task 1 复审 M-2：`(owner_kind, owner_id)` 的 active 唯一性在**库**级。

        074 只建了普通索引，`find_internal_conversation` 靠 `updated_at DESC LIMIT 1`
        隐式兜底。075 的局部唯一索引把它变成不变量（照 053 部分唯一索引的先例）。
        删掉 075 或去掉索引的 `WHERE`，下面第二条 `create_internal_conversation` 就不再抛错。
        """
        first = self.repository.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_dup",
            title="《甲》· 执行记录",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create_internal_conversation(
                owner_kind="task_execution",
                owner_id="task_dup",
                title="《甲》第二份 · 执行记录",
            )

        # 不同 owner、或不同 owner_kind → 合法（唯一性是"同一 owner 一份"）
        self.repository.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_other",
            title="《乙》· 执行记录",
        )
        self.repository.create_internal_conversation(
            owner_kind="reminder_execution",
            owner_id="task_dup",
            title="《丙》· 执行记录",
        )

        # 归档后可以再建一份（既有语义：用户删/归档执行记录 → 下次到点新建）
        self.repository.set_conversation_status(
            first.id, ConversationStatus.ARCHIVED
        )
        rebuilt = self.repository.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_dup",
            title="《甲》· 执行记录",
        )
        self.assertNotEqual(first.id, rebuilt.id)
        # 查询只认 active，所以拿到的永远是那唯一一份 active
        found = self.repository.find_internal_conversation(
            "task_execution", "task_dup"
        )
        self.assertEqual(rebuilt.id, found.id)

    def test_internal_owner_kind_check_constraint_rejects_unknown_values(self) -> None:
        """074 按 024 先例给 `internal_owner_kind` 补了 CHECK（复审 M-1）。

        取值不再是"只能靠仓储层自觉"：库里也挡。写成裸 UPDATE 才测得到约束本身。
        """
        conversation = self.repository.create_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE conversations SET internal_owner_kind = ?, "
                    "internal_owner_id = ? WHERE id = ?",
                    ("not_a_known_kind", "x", conversation.id),
                )
        # NULL / 两个合法值都放行
        for kind in (None, "task_execution", "reminder_execution"):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE conversations SET internal_owner_kind = ?, "
                    "internal_owner_id = ? WHERE id = ?",
                    (kind, None if kind is None else "x", conversation.id),
                )

    def test_internal_execution_conversation_is_never_reused_as_empty(self) -> None:
        """批次 B Task 1：内部执行会话（任务/提醒的执行记录）不参与"空会话复用"。

        它即使**还没有任何 v2 entry**也不是用户的空会话——用户点「新对话」拿到某个
        任务的执行会话是一类真实缺陷（ADR-1 §3.6）。删掉
        `create_or_reuse_empty_conversation` 里的 `internal_owner_id IS NULL` 谓词即红。
        """
        # 先占一条普通空会话：下面要证明返回的是它，而不是执行会话。
        normal = self.repository.create_or_reuse_empty_conversation()

        # 造一条"无 entry 的内部执行会话"——这正是原来会被误复用的形状。
        execution = self.repository.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_1",
            title="《每周发布检查》· 执行记录",
        )
        # 前提成立：它确实还没有任何 entry。
        with self.database.connect() as connection:
            entries = connection.execute(
                "SELECT COUNT(*) AS n FROM v2_transcript_entries "
                "WHERE conversation_id = ?",
                (execution.id,),
            ).fetchone()["n"]
        self.assertEqual(0, entries)

        reused = self.repository.create_or_reuse_empty_conversation()
        self.assertEqual(normal.id, reused.id)
        self.assertNotEqual(execution.id, reused.id)

        # 执行会话没有被复用、也没有被改写：标记仍是它自己那份记录。
        found = self.repository.find_internal_conversation("task_execution", "task_1")
        self.assertIsNotNone(found)
        self.assertEqual(execution.id, found.id)
        self.assertEqual("《每周发布检查》· 执行记录", found.title)
        self.assertEqual(ConversationKind.NORMAL, found.kind)
        self.assertIsNone(found.parent_conversation_id)
        self.assertIsNone(found.fork_turn_id)

        # 归档后查找不到 → 调用方按"新建一份"继续（ADR §5 登记的已知边界）。
        self.repository.set_conversation_status(
            execution.id, ConversationStatus.ARCHIVED
        )
        self.assertIsNone(
            self.repository.find_internal_conversation("task_execution", "task_1")
        )

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
