from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from endless_task.api import AppSettings
from endless_task.cli import main
from endless_task.storage import Database, SqliteChatRepository


class RuntimeV2MigrationCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cli.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.repository = SqliteChatRepository(self.database)
        conversation = self.repository.create_conversation()
        self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="迁移测试",
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_cli(
        self,
        arguments: list[str],
        *,
        expected_exit_code: int = 0,
    ) -> str:
        settings = AppSettings(database_path=self.database_path)
        output = io.StringIO()
        with mock.patch.object(AppSettings, "from_environment", return_value=settings):
            with redirect_stdout(output):
                exit_code = main(arguments)
        self.assertEqual(expected_exit_code, exit_code)
        return output.getvalue()

    def test_dry_run_does_not_modify_source_database(self) -> None:
        output = self._run_cli(["migrate-runtime-v2", "--dry-run"])

        self.assertIn("mode=dry-run", output)
        self.assertIn("source_unchanged=true", output)
        self.assertIn("conversations=1", output)
        self.assertIn("runs=1", output)
        with self.database.connect() as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM v2_migration_state").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM v2_transcript_entries").fetchone()[0],
            )

    def test_audit_before_migration_fails_read_only(self) -> None:
        output = self._run_cli(
            ["migrate-runtime-v2", "--audit"],
            expected_exit_code=1,
        )

        self.assertIn("mode=audit", output)
        self.assertIn("passed=false", output)
        self.assertIn("migration_state=not_migrated", output)
        self.assertIn("migration_state_missing", output)

    def test_apply_backs_up_and_migrates_source_database(self) -> None:
        output = self._run_cli(["migrate-runtime-v2", "--apply"])

        self.assertIn("mode=apply", output)
        self.assertIn("conversations=1", output)
        self.assertIn("runs=1", output)
        self.assertIn("backup=", output)
        with self.database.connect() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM v2_migration_state").fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute("SELECT COUNT(*) FROM v2_transcript_entries").fetchone()[0],
            )

        backup_line = next(
            line for line in output.splitlines() if line.startswith("backup=")
        )
        backup_path = Path(backup_line.split("=", 1)[1])
        self.assertTrue(backup_path.is_file())
        self.assertEqual("ok", Database(backup_path).integrity_check())

        audit_output = self._run_cli(["migrate-runtime-v2", "--audit"])
        self.assertIn("passed=true", audit_output)
        self.assertIn("migration_state=migrated", audit_output)
        self.assertIn("conversation_mappings=1", audit_output)
        self.assertIn("pending_migration_conversations=0", audit_output)
        self.assertIn("rollback_reconciliation_trees=0", audit_output)

        conversation = self.repository.list_conversations()[0]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE turns SET status = 'completed' WHERE conversation_id = ?",
                (conversation.id,),
            )
        self.repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="rollback-request",
            content="回滚期间新增",
        )
        reconciled_output = self._run_cli(
            ["migrate-runtime-v2", "--audit"],
            expected_exit_code=1,
        )
        self.assertIn("passed=false", reconciled_output)
        self.assertIn("rollback_reconciliation_trees=1", reconciled_output)
        self.assertIn(
            "rollback_reconciliation_required_count=1",
            reconciled_output,
        )

    def test_requires_an_explicit_mode(self) -> None:
        settings = AppSettings(database_path=self.database_path)
        output = io.StringIO()
        with mock.patch.object(AppSettings, "from_environment", return_value=settings):
            with redirect_stdout(output):
                exit_code = main(["migrate-runtime-v2"])
        self.assertEqual(2, exit_code)
        self.assertIn("请指定 --dry-run、--apply 或 --audit", output.getvalue())
