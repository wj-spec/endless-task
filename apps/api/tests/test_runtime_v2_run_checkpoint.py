"""M3B run-level: RunCheckpointCoordinator (方案 A) tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.execution_env.checkpoint import plan_restore
from endless_task.execution_env.ledger import (
    FileMutationLedger,
    LedgerEntry,
    ledger_timestamp,
)
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator


class RunCheckpointCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self.store = self.base / "checkpoints"
        self.ledger_path = self.base / "ledger.jsonl"
        self.ledger = FileMutationLedger(self.ledger_path)
        self.coordinator = RunCheckpointCoordinator(
            store_root=self.store,
            ledger=self.ledger,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent_write(self, relative: str, content: str) -> None:
        """Simulate an agent tool write: mutate file + record in ledger."""
        target = self.ws / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_bytes() if target.exists() else None
        target.write_text(content, encoding="utf-8")
        import hashlib

        def sha(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        self.ledger.append(
            LedgerEntry(
                effect_id=f"fx_{relative}",
                path=relative,
                operation="file_write",
                timestamp=ledger_timestamp(),
                before_hash=sha(before) if before is not None else None,
                after_hash=sha(content.encode("utf-8")),
            )
        )

    def _user_write(self, relative: str, content: str) -> None:
        target = self.ws / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_ensure_checkpoint_idempotent_per_run_workspace(self) -> None:
        (self.ws / "keep.txt").write_text("original", encoding="utf-8")
        first = self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        second = self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertEqual(first.checkpoint_id, second.checkpoint_id)
        self.assertEqual(("run_1",), self.coordinator.tracked_run_ids())

    def test_restore_reverts_agent_changes_keeps_user_changes(self) -> None:
        (self.ws / "agent.txt").write_text("original-agent", encoding="utf-8")
        (self.ws / "user.txt").write_text("original-user", encoding="utf-8")
        self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        # Agent modifies agent.txt; user concurrently edits user.txt.
        self._agent_write("agent.txt", "agent-changed")
        self._user_write("user.txt", "user-changed")

        restored, skipped = self.coordinator.apply_restore(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertIn("agent.txt", restored)
        self.assertIn("user.txt", skipped)  # user change never overwritten
        self.assertEqual(
            "original-agent",
            (self.ws / "agent.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "user-changed",
            (self.ws / "user.txt").read_text(encoding="utf-8"),
        )

    def test_new_files_after_checkpoint_untouched(self) -> None:
        (self.ws / "base.txt").write_text("base", encoding="utf-8")
        self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self._agent_write("created.txt", "new-agent-file")
        restored, skipped = self.coordinator.apply_restore(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertEqual((), restored)
        self.assertIn("created.txt", skipped)  # created after cp: untouched
        self.assertTrue((self.ws / "created.txt").exists())

    def test_restore_without_checkpoint_fails_closed(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.coordinator.apply_restore(
                run_id="run_x", workspace_root=str(self.ws)
            )
        self.assertEqual("checkpoint_not_found", caught.exception.code)

    def test_plan_restore_dry_run_does_not_mutate(self) -> None:
        (self.ws / "agent.txt").write_text("original", encoding="utf-8")
        self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self._agent_write("agent.txt", "changed")
        plan = self.coordinator.plan_restore(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertIn("agent.txt", plan.would_apply)
        # Dry run leaves the file changed.
        self.assertEqual(
            "changed",
            (self.ws / "agent.txt").read_text(encoding="utf-8"),
        )

    def test_multiple_workspaces_per_run_tracked_independently(self) -> None:
        ws2 = self.base / "ws2"
        ws2.mkdir()
        r1 = self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(self.ws)
        )
        r2 = self.coordinator.ensure_checkpoint(
            run_id="run_1", workspace_root=str(ws2)
        )
        self.assertNotEqual(r1.checkpoint_id, r2.checkpoint_id)
        self.assertEqual(2, len(self.coordinator._checkpoints["run_1"]))


if __name__ == "__main__":
    unittest.main()
