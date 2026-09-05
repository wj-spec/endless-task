"""M6 W6-6: explicit trajectory-export CLI entry (08 §OE-3 explicit path)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from endless_task.api import AppSettings
from endless_task.cli import main
from endless_task.runtime_v2 import Actor, TranscriptEntryType
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository


class TrajectoryExportCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cli.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

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

    def _create_terminal_run(self) -> str:
        from endless_task.runtime_v2 import RunStatus

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "hi"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        # A failed terminal run with a journal event.
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_failed",
            payload={"errorCode": "provider_timeout"},
        )
        self.repository.transition_run_status(
            run.id,
            RunStatus.FAILED,
            event_type="run_failed",
            error_code="provider_timeout",
            safe_message="模型调用超时。",
        )
        return run.id

    def test_explicit_export_writes_bundle_directory(self) -> None:
        run_id = self._create_terminal_run()
        output = self._run_cli(["trajectory-export", run_id])
        directory = Path(output.strip())
        self.assertTrue(directory.is_dir(), output)
        self.assertEqual(
            f"trajectory-{run_id}",
            directory.name,
        )
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(run_id, manifest["sourceRunId"])
        self.assertTrue((directory / "events.jsonl").exists())

    def test_explicit_export_unknown_run_fails(self) -> None:
        output = self._run_cli(
            ["trajectory-export", "run_no_such_run"],
            expected_exit_code=1,
        )
        self.assertIn("failed to export", output)


if __name__ == "__main__":
    unittest.main()


class RetentionCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cli.db"
        self.database = Database(self.database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_cli(self, arguments, *, expected_exit_code=0) -> str:
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        settings = AppSettings(database_path=self.database_path)
        output = io.StringIO()
        with mock.patch.object(AppSettings, "from_environment", return_value=settings):
            with redirect_stdout(output):
                exit_code = main(arguments)
        self.assertEqual(expected_exit_code, exit_code)
        return output.getvalue()

    def test_retention_dry_run_default(self) -> None:
        output = self._run_cli(["retention"])
        self.assertIn("dry_run=true", output)
        self.assertIn("spans=0 events=0 usage=0", output)

    def test_retention_apply_on_empty_db(self) -> None:
        output = self._run_cli(["retention", "--apply"])
        self.assertIn("dry_run=false", output)
        self.assertIn("spans=0 events=0 usage=0", output)

    def test_retention_cleans_trajectory_bundles(self) -> None:
        export_root = self.database_path.parent / "v2_trajectory_exports"
        for index in range(3):
            (export_root / f"trajectory-run_{index}").mkdir(parents=True)
        output = self._run_cli(
            ["retention", "--keep-bundles", "1", "--apply"]
        )
        self.assertIn("dry_run=false", output)
        self.assertIn("trajectory_bundles=2", output)
        remaining = list(export_root.iterdir())
        self.assertEqual(1, len(remaining))


class AgentFlagsCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cli.db"
        self.database = Database(self.database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_cli(self, arguments, *, expected_exit_code=0) -> str:
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        settings = AppSettings(database_path=self.database_path)
        output = io.StringIO()
        with mock.patch.object(AppSettings, "from_environment", return_value=settings):
            with redirect_stdout(output):
                exit_code = main(arguments)
        self.assertEqual(expected_exit_code, exit_code)
        return output.getvalue()

    def test_flags_lists_platform_flags(self) -> None:
        output = self._run_cli(["flags"])
        self.assertIn("ENDLESS_TASK_DELEGATION=readonly", output)
        self.assertIn("ENDLESS_TASK_RUNTIME_TRACE=all", output)
        self.assertNotIn("(未接线)", output)
        self.assertIn("ENDLESS_TASK_STOP_POLICY_V2", output)
        self.assertIn("ENDLESS_TASK_EXECUTION_BACKEND=local", output)

    def test_rollback_dry_run_wired_flag(self) -> None:
        output = self._run_cli(
            ["flags", "--rollback-dry-run", "ENDLESS_TASK_RUNTIME_TRACE"]
        )
        self.assertIn("wired=true", output)
        self.assertIn("container_built=true", output)
        self.assertIn("before=all", output)
        self.assertIn("after=0", output)

    def test_rollback_dry_run_delegation(self) -> None:
        output = self._run_cli(
            ["flags", "--rollback-dry-run", "ENDLESS_TASK_DELEGATION"]
        )
        self.assertIn("wired=true", output)
        self.assertIn("container_built=true", output)
        self.assertIn("after=0", output)

    def test_rollback_dry_run_stop_policy_flag(self) -> None:
        output = self._run_cli(
            ["flags", "--rollback-dry-run", "ENDLESS_TASK_STOP_POLICY_V2"]
        )
        self.assertIn("wired=true", output)
        self.assertIn("container_built=true", output)
        self.assertIn("before=False", output)

    def test_rollback_dry_run_unknown_flag_fails(self) -> None:
        output = self._run_cli(
            ["flags", "--rollback-dry-run", "ENDLESS_TASK_BOGUS"],
            expected_exit_code=0,  # unwired path: no-op, exit 0
        )
        self.assertIn("wired=false", output)
