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


class RunCheckpointPersistenceTest(unittest.TestCase):
    """M3B slice C: durable ref index survives coordinator/process restart."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self.store = self.base / "checkpoints"
        self.ledger_path = self.base / "ledger.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _coordinator(self) -> RunCheckpointCoordinator:
        return RunCheckpointCoordinator(
            store_root=self.store,
            ledger=FileMutationLedger(self.ledger_path),
        )

    def test_refs_reload_across_coordinators_and_restore_still_works(self) -> None:
        (self.ws / "task.txt").write_text("original", encoding="utf-8")
        first = self._coordinator()
        ref = first.ensure_checkpoint(run_id="run_1", workspace_root=str(self.ws))

        # "Restart": a brand-new coordinator over the same store + ledger.
        second = self._coordinator()
        self.assertEqual((ref.run_id,), second.tracked_run_ids())
        reloaded = second.checkpoint_for(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertIsNotNone(reloaded)
        self.assertEqual(ref.checkpoint_id, reloaded.checkpoint_id)

        # Agent write after restart is recorded on the durable ledger and the
        # reloaded ref restores it (cross-process semantics).
        (self.ws / "task.txt").write_text("agent-changed", encoding="utf-8")
        import hashlib

        after = hashlib.sha256(b"agent-changed").hexdigest()
        second.record_effect(
            effect_id="fx_post_restart",
            path="task.txt",
            operation="file_write",
            before_hash=hashlib.sha256(b"original").hexdigest(),
            after_hash=after,
        )
        restored, skipped = second.apply_restore(
            run_id="run_1", workspace_root=str(self.ws)
        )
        self.assertIn("task.txt", restored)
        self.assertNotIn("task.txt", skipped)
        self.assertEqual(
            "original", (self.ws / "task.txt").read_text(encoding="utf-8")
        )

    def test_corrupt_index_rows_are_skipped(self) -> None:
        first = self._coordinator()
        r1 = first.ensure_checkpoint(run_id="run_1", workspace_root=str(self.ws))
        ws2 = self.base / "ws2"
        ws2.mkdir()
        r2 = first.ensure_checkpoint(run_id="run_2", workspace_root=str(ws2))
        # Torn tail after a crash.
        with open(self.store / RunCheckpointCoordinator._INDEX_NAME, "a", encoding="utf-8") as h:
            h.write("{not-json\n")
        second = self._coordinator()
        self.assertEqual({"run_1", "run_2"}, set(second.tracked_run_ids()))
        self.assertEqual(
            r1.checkpoint_id,
            second.checkpoint_for(run_id="run_1", workspace_root=str(self.ws)).checkpoint_id,
        )
        self.assertEqual(
            r2.checkpoint_id,
            second.checkpoint_for(run_id="run_2", workspace_root=str(ws2)).checkpoint_id,
        )

    def test_empty_store_loads_empty_index(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual((), coordinator.tracked_run_ids())

    def test_ensure_is_idempotent_in_index(self) -> None:
        first = self._coordinator()
        first.ensure_checkpoint(run_id="run_1", workspace_root=str(self.ws))
        first.ensure_checkpoint(run_id="run_1", workspace_root=str(self.ws))
        lines = [
            line
            for line in (
                self.store / RunCheckpointCoordinator._INDEX_NAME
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(lines))

    def test_store_under_workspace_is_never_snapshotted(self) -> None:
        nested_store = self.ws / "cp-store"
        nested_store.mkdir(parents=True, exist_ok=True)
        (nested_store / "secret.txt").write_text("store-content", encoding="utf-8")
        (self.ws / "user.txt").write_text("user-content", encoding="utf-8")
        coordinator = RunCheckpointCoordinator(store_root=nested_store)
        coordinator.ensure_checkpoint(run_id="run_1", workspace_root=str(self.ws))
        manifest = coordinator._load_manifest(
            coordinator.checkpoint_for(run_id="run_1", workspace_root=str(self.ws))
        )
        paths = {relative for relative, _ in manifest.entries}
        self.assertIn("user.txt", paths)
        self.assertFalse(
            any(path.startswith("cp-store") for path in paths),
            f"store dir leaked into snapshot: {sorted(paths)}",
        )


if __name__ == "__main__":
    unittest.main()
