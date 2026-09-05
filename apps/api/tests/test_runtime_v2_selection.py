from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ConversationKind
from endless_task.runtime_v2 import (
    RuntimeV2MigrationService,
    RuntimeV2RuntimeSelectionService,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)


class RuntimeV2SelectionServiceTest(unittest.TestCase):
    """14 A2: v2 sole runtime — selection is a v2-only status service."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "selection.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self) -> RuntimeV2RuntimeSelectionService:
        return RuntimeV2RuntimeSelectionService(
            database=self.database,
            repository=self.repository,
        )

    def test_empty_conversation_is_v2(self) -> None:
        conversation = self.chat_repository.create_conversation()
        service = self._service()

        status = service.describe(conversation.id)
        self.assertEqual("v2", status.effective_runtime)
        self.assertEqual("v2_sole_runtime", status.reason)
        self.assertIsNone(status.override_runtime)
        self.assertFalse(status.rollback_forced)
        self.assertTrue(status.can_use_v2)
        self.assertFalse(status.requires_migration)
        self.assertFalse(status.v1_read_only)
        self.assertEqual(conversation.id, status.tree_conversation_id)

    def test_unmigrated_history_is_v2_by_decision(self) -> None:
        # v1 引擎已删：即便存在未迁移旧历史，决策(14)也不允许 v1——describe 恒 v2。
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="旧会话",
        )
        service = self._service()

        status = service.describe(conversation.id)
        self.assertEqual("v2", status.effective_runtime)
        self.assertTrue(status.can_use_v2)
        self.assertFalse(status.requires_migration)

    def test_global_status_reports_v2_default_with_zero_reconciliation(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="历史",
        )
        service = self._service()
        status = service.global_status()
        self.assertEqual("v2", status.default_runtime)
        self.assertFalse(status.rollback_forced)
        self.assertEqual(0, status.rollback_reconciliation_count)
        self.assertEqual(1, status.conversation_count)
        # 未迁移旧历史在 B 阶段删表前仍计入 pending（诊断用）。
        self.assertEqual(1, status.pending_migration_count)

    def test_describe_unknown_conversation_raises(self) -> None:
        service = self._service()
        with self.assertRaises(Exception):
            service.describe("no_such_conversation")

    def test_migrated_persistent_branch_resolves_to_parent_tree(self) -> None:
        parent = self.chat_repository.create_conversation()
        turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="request-1",
            content="主会话",
        )
        branch = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=turn.turn.id,
            kind=ConversationKind.NORMAL,
        )
        self.chat_repository.create_turn(
            conversation_id=branch.id,
            client_request_id="request-2",
            content="分支会话",
        )
        RuntimeV2MigrationService(self.database).migrate()
        service = self._service()

        parent_status = service.describe(parent.id)
        branch_status = service.describe(branch.id)
        self.assertEqual("v2", parent_status.effective_runtime)
        self.assertEqual("v2", branch_status.effective_runtime)
        self.assertEqual(parent.id, branch_status.tree_conversation_id)
        self.assertFalse(branch_status.v1_read_only)
        self.assertFalse(branch_status.requires_migration)

        global_status = service.global_status()
        self.assertEqual("migrated", global_status.migration_state)
        self.assertEqual(2, global_status.conversation_count)
        self.assertEqual(2, global_status.mapped_conversation_count)
        self.assertEqual(1, global_status.conversation_tree_count)
        self.assertEqual(0, global_status.pending_migration_count)

    def test_migrated_legacy_ephemeral_branch_resolves_to_own_tree(self) -> None:
        parent = self.chat_repository.create_conversation()
        turn = self.chat_repository.create_turn(
            conversation_id=parent.id,
            client_request_id="request-1",
            content="主会话",
        )
        temporary = self.chat_repository.create_branch(
            parent_conversation_id=parent.id,
            fork_turn_id=turn.turn.id,
        )
        self.chat_repository.create_turn(
            conversation_id=temporary.id,
            client_request_id="request-2",
            content="临时会话",
        )
        RuntimeV2MigrationService(self.database).migrate()
        service = self._service()

        parent_status = service.describe(parent.id)
        temporary_status = service.describe(temporary.id)
        self.assertEqual("v2", parent_status.effective_runtime)
        self.assertEqual("v2", temporary_status.effective_runtime)
        self.assertEqual(temporary.id, temporary_status.tree_conversation_id)

    def test_migration_audit_reconciliation_helper_is_zero_after_clean_migrate(
        self,
    ) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="迁移前会话",
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE turns SET status = 'completed' WHERE conversation_id = ?",
                (conversation.id,),
            )
        RuntimeV2MigrationService(self.database).migrate()
        report = RuntimeV2MigrationService(self.database).audit()
        self.assertGreaterEqual(report.mapped_conversation_count, 1)
        self.assertEqual(0, report.rollback_reconciliation_count)


if __name__ == "__main__":
    unittest.main()
