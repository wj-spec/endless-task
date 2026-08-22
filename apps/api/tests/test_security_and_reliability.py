from __future__ import annotations

import io
import logging
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from endless_task.api import AppSettings
from endless_task.api.app import CONFIG_VERSION
from endless_task.security import RedactingFormatter
from endless_task.storage import Database, SqliteChatRepository


class SecurityAndReliabilityTest(unittest.TestCase):
    def test_redacting_formatter_removes_known_and_structural_secrets(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            RedactingFormatter("%(message)s", secrets=("deepseek-secret-value",))
        )
        logger = logging.getLogger("endless_task.tests.redaction")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        try:
            raise RuntimeError(
                "api_key=deepseek-secret-value Authorization: Bearer sk-example123"
            )
        except RuntimeError:
            logger.exception("provider failed with token=private-token")

        output = stream.getvalue()
        self.assertNotIn("deepseek-secret-value", output)
        self.assertNotIn("sk-example123", output)
        self.assertNotIn("private-token", output)
        self.assertIn("[REDACTED]", output)

    def test_configuration_is_versioned_and_uses_explicit_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "ENDLESS_TASK_CONFIG_VERSION": str(CONFIG_VERSION),
                    "ENDLESS_TASK_DATA_DIR": directory,
                },
                clear=True,
            ):
                settings = AppSettings.from_environment()

            self.assertEqual(CONFIG_VERSION, settings.config_version)
            self.assertEqual(Path(directory) / "endless-task.db", settings.database_path)

        with self.assertRaises(ValueError):
            AppSettings(
                database_path=Path("invalid.db"),
                config_version=CONFIG_VERSION + 1,
            )

    def test_online_backup_is_complete_private_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source" / "endless-task.db"
            backup_path = Path(directory) / "backup" / "snapshot.db"
            database = Database(source_path)
            database.initialize()
            repository = SqliteChatRepository(database)
            conversation = repository.create_conversation()

            created = database.backup(backup_path)

            self.assertEqual(backup_path.resolve(), created)
            self.assertEqual("ok", Database(backup_path).integrity_check())
            with closing(sqlite3.connect(backup_path)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            self.assertEqual(1, count)
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(backup_path.stat().st_mode))
            with self.assertRaises(FileExistsError):
                database.backup(backup_path)
            with self.assertRaises(ValueError):
                database.backup(source_path)
            self.assertTrue(conversation.id)


if __name__ == "__main__":
    unittest.main()
