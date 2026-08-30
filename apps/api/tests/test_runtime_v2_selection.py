from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ConversationKind
from endless_task.domain.repositories import ConflictError
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
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "selection.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(
        self,
        default_runtime: str = "v1",
        rollback_forced: bool = False,
    ) -> RuntimeV2RuntimeSelectionService:
        return RuntimeV2RuntimeSelectionService(
            database=self.database,
            repository=self.repository,
            default_runtime=default_runtime,
            rollback_forced=rollback_forced,
        )

    def test_empty_conversation_can_override_to_v2(self) -> None:
        conversation = self.chat_repository.create_conversation()
        service = self._service("v1")

        initial = service.describe(conversation.id)
        self.assertEqual("v1", initial.effective_runtime)
        self.assertTrue(initial.can_use_v2)
        self.assertFalse(initial.requires_migration)

        updated = service.set_override(conversation.id, runtime="v2")
        self.assertEqual("v2", updated.effective_runtime)
        self.assertEqual("v2", updated.override_runtime)
        self.assertEqual("conversation_override", updated.reason)

    def test_unmigrated_conversation_with_history_cannot_use_v2(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="旧会话",
        )
        service = self._service("v2")

        status = service.describe(conversation.id)
        self.assertEqual("v1", status.effective_runtime)
        self.assertFalse(status.can_use_v2)
        self.assertTrue(status.requires_migration)
        self.assertEqual("migration_required", status.reason)
        with self.assertRaises(ConflictError):
            service.set_override(conversation.id, runtime="v2")

    def test_global_v1_mode_forces_rollback(self) -> None:
        conversation = self.chat_repository.create_conversation()
        service = self._service("v1", rollback_forced=True)
        service.set_override(conversation.id, runtime="v2")

        status = service.describe(conversation.id)
        self.assertEqual("v1", status.effective_runtime)
        self.assertEqual("global_v1_rollback", status.reason)
        self.assertEqual("v2", status.override_runtime)

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
        service = self._service("v2")

        parent_status = service.describe(parent.id)
        branch_status = service.describe(branch.id)
        self.assertEqual("v2", parent_status.effective_runtime)
        self.assertEqual("v2", branch_status.effective_runtime)
        self.assertEqual(parent.id, branch_status.tree_conversation_id)
        self.assertTrue(branch_status.v1_read_only)
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
        service = self._service("v2")

        parent_status = service.describe(parent.id)
        temporary_status = service.describe(temporary.id)
        self.assertEqual("v2", parent_status.effective_runtime)
        self.assertEqual("v2", temporary_status.effective_runtime)
        self.assertEqual(temporary.id, temporary_status.tree_conversation_id)
        self.assertTrue(temporary_status.v1_read_only)
        self.assertFalse(temporary_status.requires_migration)

        global_status = service.global_status()
        self.assertEqual("migrated", global_status.migration_state)
        self.assertEqual(2, global_status.conversation_count)
        self.assertEqual(2, global_status.mapped_conversation_count)
        self.assertEqual(2, global_status.conversation_tree_count)
        self.assertEqual(0, global_status.pending_migration_count)

    def test_migrated_conversation_v1_write_is_read_only(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="旧会话",
        )
        RuntimeV2MigrationService(self.database).migrate()
        service = self._service("v1")

        status = service.describe(conversation.id)
        self.assertTrue(status.v1_read_only)
        with self.assertRaises(ConflictError):
            service.ensure_v1_write_allowed(conversation.id)
        with self.assertRaises(ConflictError):
            service.set_override(conversation.id, runtime="v1")

    def test_global_rollback_reopens_migrated_v1_write(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="旧会话",
        )
        RuntimeV2MigrationService(self.database).migrate()
        service = self._service("v1", rollback_forced=True)

        service.ensure_v1_write_allowed(conversation.id)
        updated = service.set_override(conversation.id, runtime="v1")
        self.assertEqual("v1", updated.effective_runtime)

    def test_v1_write_during_rollback_requires_reconciliation_before_v2(self) -> None:
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
        rollback_service = self._service("v2", rollback_forced=True)
        rollback_service.ensure_v1_write_allowed(conversation.id)
        rollback_turn = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="rollback-request",
            content="回滚期间新增",
        )
        self.assertIsNotNone(rollback_turn)

        service = self._service("v2")
        status = service.describe(conversation.id)
        self.assertFalse(status.can_use_v2)
        self.assertTrue(status.requires_migration)
        self.assertTrue(status.rollback_reconciliation_required)
        self.assertEqual("rollback_reconciliation_required", status.reason)
        with self.assertRaises(ConflictError):
            service.set_override(conversation.id, runtime="v2")

        global_status = service.global_status()
        self.assertEqual(1, global_status.rollback_reconciliation_count)


if __name__ == "__main__":
    unittest.main()
