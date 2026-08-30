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
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE v2_message_requests")
            connection.execute(
                "DELETE FROM schema_migrations WHERE name IN (?, ?, ?)",
                (
                    "043_runtime_v2_message_idempotency.sql",
                    "044_runtime_v2_main_lane_pointer.sql",
                    "045_runtime_v2_promoted_main_lane_repair.sql",
                ),
            )
        migrations_before = self.database.applied_migrations()

        output = self._run_cli(["migrate-runtime-v2", "--dry-run"])

        self.assertIn("mode=dry-run", output)
        self.assertIn("source_unchanged=true", output)
        self.assertIn("conversations=1", output)
        self.assertIn("runs=1", output)
        self.assertEqual(migrations_before, self.database.applied_migrations())
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'v2_message_requests'"
                ).fetchone()
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM v2_migration_state").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM v2_transcript_entries").fetchone()[0],
            )

    def test_audit_before_migration_fails_read_only(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE v2_message_requests")
            connection.execute(
                "DELETE FROM schema_migrations WHERE name IN (?, ?, ?)",
                (
                    "043_runtime_v2_message_idempotency.sql",
                    "044_runtime_v2_main_lane_pointer.sql",
                    "045_runtime_v2_promoted_main_lane_repair.sql",
                ),
            )
        migrations_before = self.database.applied_migrations()

        output = self._run_cli(
            ["migrate-runtime-v2", "--audit"],
            expected_exit_code=1,
        )

        self.assertIn("mode=audit", output)
        self.assertIn("source_unchanged=true", output)
        self.assertIn("passed=false", output)
        self.assertIn("migration_state=not_migrated", output)
        self.assertIn("migration_state_missing", output)
        self.assertEqual(migrations_before, self.database.applied_migrations())
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'v2_message_requests'"
                ).fetchone()
            )

    def test_apply_backs_up_and_migrates_source_database(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE v2_message_requests")
            connection.execute(
                "DELETE FROM schema_migrations WHERE name IN (?, ?, ?)",
                (
                    "043_runtime_v2_message_idempotency.sql",
                    "044_runtime_v2_main_lane_pointer.sql",
                    "045_runtime_v2_promoted_main_lane_repair.sql",
                ),
            )

        output = self._run_cli(["migrate-runtime-v2", "--apply"])

        self.assertIn("mode=apply", output)
        self.assertIn("migrated=true", output)
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
        backup_database = Database(backup_path)
        self.assertEqual("ok", backup_database.integrity_check())
        self.assertNotIn(
            "043_runtime_v2_message_idempotency.sql",
            backup_database.applied_migrations(),
        )
        self.assertNotIn(
            "044_runtime_v2_main_lane_pointer.sql",
            backup_database.applied_migrations(),
        )
        self.assertNotIn(
            "045_runtime_v2_promoted_main_lane_repair.sql",
            backup_database.applied_migrations(),
        )
        self.assertIn(
            "044_runtime_v2_main_lane_pointer.sql",
            self.database.applied_migrations(),
        )
        self.assertIn(
            "045_runtime_v2_promoted_main_lane_repair.sql",
            self.database.applied_migrations(),
        )

        repeated_output = self._run_cli(["migrate-runtime-v2", "--apply"])
        self.assertIn("already_migrated=true", repeated_output)
        with self.database.connect() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM v2_migration_state").fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute("SELECT COUNT(*) FROM v2_transcript_entries").fetchone()[0],
            )

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

    def test_apply_failure_reports_pre_upgrade_backup(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE v2_message_requests")
            connection.execute(
                "DELETE FROM schema_migrations WHERE name IN (?, ?, ?)",
                (
                    "043_runtime_v2_message_idempotency.sql",
                    "044_runtime_v2_main_lane_pointer.sql",
                    "045_runtime_v2_promoted_main_lane_repair.sql",
                ),
            )

        with mock.patch(
            "endless_task.runtime_v2.RuntimeV2MigrationService.migrate",
            side_effect=RuntimeError("injected migration failure"),
        ):
            output = self._run_cli(
                ["migrate-runtime-v2", "--apply"],
                expected_exit_code=1,
            )

        self.assertIn("mode=apply", output)
        self.assertIn("migrated=false", output)
        self.assertIn("error=injected migration failure", output)
        backup_line = next(
            line for line in output.splitlines() if line.startswith("backup=")
        )
        backup = Database(Path(backup_line.split("=", 1)[1]))
        self.assertEqual("ok", backup.integrity_check())
        self.assertNotIn(
            "043_runtime_v2_message_idempotency.sql",
            backup.applied_migrations(),
        )

    def test_restore_requires_confirmation(self) -> None:
        source = Path(self._temporary_directory.name) / "restore-source.db"
        self.database.backup(source)

        output = self._run_cli(
            ["restore", str(source)],
            expected_exit_code=2,
        )

        self.assertIn("请停止应用并显式传入 --confirm", output)
        self.assertEqual(1, len(self.repository.list_conversations()))

    def test_restore_preserves_current_database_as_safety_backup(self) -> None:
        source = Path(self._temporary_directory.name) / "restore-source.db"
        self.database.backup(source)
        self.repository.create_conversation()

        output = self._run_cli(["restore", str(source), "--confirm"])

        self.assertIn("mode=restore", output)
        self.assertIn("integrity_check=ok", output)
        self.assertIn("restored=true", output)
        self.assertEqual(
            1,
            len(SqliteChatRepository(self.database).list_conversations()),
        )
        safety_backup_line = next(
            line for line in output.splitlines() if line.startswith("safety_backup=")
        )
        safety_backup = Path(safety_backup_line.split("=", 1)[1])
        self.assertTrue(safety_backup.is_file())
        self.assertEqual(
            2,
            len(SqliteChatRepository(Database(safety_backup)).list_conversations()),
        )

    def test_restore_rejects_corrupt_source_without_replacing_live_database(self) -> None:
        source = Path(self._temporary_directory.name) / "corrupt.db"
        source.write_bytes(b"not a sqlite database")

        output = self._run_cli(
            ["restore", str(source), "--confirm"],
            expected_exit_code=1,
        )

        self.assertIn("mode=restore", output)
        self.assertIn("restored=false", output)
        self.assertEqual(1, len(self.repository.list_conversations()))

    def test_requires_an_explicit_mode(self) -> None:
        settings = AppSettings(database_path=self.database_path)
        output = io.StringIO()
        with mock.patch.object(AppSettings, "from_environment", return_value=settings):
            with redirect_stdout(output):
                exit_code = main(["migrate-runtime-v2"])
        self.assertEqual(2, exit_code)
        self.assertIn("请指定 --dry-run、--apply 或 --audit", output.getvalue())
