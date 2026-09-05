"""AP-305: content-addressed checkpoint, guarded restore and mutation ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import EffectOutcome
from endless_task.execution_env import (
    ExecutionPolicy,
    FileMutationOperation,
    FileMutationRequest,
    LocalExecutionBackend,
    ReadFileRequest,
    CheckpointRef,
    CheckpointRequest,
    RestoreRequest,
)
from endless_task.execution_env.ledger import FileMutationLedger, LedgerEntry, ledger_timestamp
from endless_task.runtime_ledger import TraceContext

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")


class CheckpointHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.store = Path(self._tmp.name) / "store"
        self.ledger_path = Path(self._tmp.name) / "ledger.jsonl"
        self.ledger = FileMutationLedger(self.ledger_path)
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
        )
        self.backend = LocalExecutionBackend(
            checkpoint_store=self.store,
            ledger=self.ledger,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _checkpoint_request(self) -> CheckpointRequest:
        return CheckpointRequest(
            run_id="run_1",
            policy=self.policy,
            trace=TRACE,
            created_at="2026-09-03T00:00:00Z",
        )

    def _mutate(self, path: str, operation, content=None, effect_id="effect_x"):
        return FileMutationRequest(
            effect_id=effect_id,
            tool_call_id="call_x",
            path=path,
            operation=operation,
            policy=self.policy,
            trace=TRACE,
            requested_at="2026-09-03T00:00:00Z",
            content=content,
        )

    def _restore(self, ref: CheckpointRef, *, dry_run: bool) -> RestoreRequest:
        return RestoreRequest(checkpoint=ref, policy=self.policy, trace=TRACE, dry_run=dry_run)


class CheckpointTest(CheckpointHarness):
    async def test_checkpoint_snapshots_and_restores_agent_changes(self) -> None:
        (self.root / "a.txt").write_text("original-a", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_text("original-b", encoding="utf-8")
        ref = await self.backend.checkpoint(self._checkpoint_request())
        self.assertEqual("run_1", ref.checkpoint_id)
        self.assertEqual("local-content", ref.backend)
        self.assertEqual(64, len(ref.root_fingerprint))

        # Agent mutations after the checkpoint (ledger records them).
        await self.backend.mutate_file(
            self._mutate("a.txt", FileMutationOperation.WRITE, content="changed-a")
        )
        (self.root / "sub" / "b.txt").unlink()
        (self.root / "new.txt").write_text("brand new", encoding="utf-8")

        dry = await self.backend.restore(self._restore(ref, dry_run=True))
        self.assertFalse(dry.applied)
        self.assertIn("a.txt", dry.restored_paths)
        self.assertIn("sub/b.txt", dry.restored_paths)
        self.assertNotIn("new.txt", dry.restored_paths)

        result = await self.backend.restore(self._restore(ref, dry_run=False))
        self.assertTrue(result.applied)
        self.assertIn("a.txt", result.restored_paths)
        self.assertEqual("original-a", (self.root / "a.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "sub" / "b.txt").exists())
        self.assertEqual("original-b", (self.root / "sub" / "b.txt").read_text(encoding="utf-8"))
        # Files created after the checkpoint are untouched.
        self.assertTrue((self.root / "new.txt").exists())

    async def test_user_modifications_are_never_overwritten(self) -> None:
        (self.root / "user.txt").write_text("checkpoint-state", encoding="utf-8")
        ref = await self.backend.checkpoint(self._checkpoint_request())
        # User edits the file directly (no agent ledger record).
        (self.root / "user.txt").write_text("user-edited", encoding="utf-8")

        dry = await self.backend.restore(self._restore(ref, dry_run=True))
        self.assertIn("user.txt", dry.skipped_paths)
        self.assertNotIn("user.txt", dry.restored_paths)

        result = await self.backend.restore(self._restore(ref, dry_run=False))
        self.assertEqual("user-edited", (self.root / "user.txt").read_text(encoding="utf-8"))
        self.assertIn("user.txt", result.skipped_paths)

    async def test_unconfigured_store_fails_closed(self) -> None:
        bare = LocalExecutionBackend()
        with self.assertRaises(Exception) as caught:
            await bare.checkpoint(self._checkpoint_request())
        self.assertEqual("not_implemented_in_slice", getattr(caught.exception, "code", None))


class LedgerTest(unittest.TestCase):
    def test_ledger_records_and_reads_back_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = FileMutationLedger(Path(directory) / "ledger.jsonl")
            ledger.append(
                LedgerEntry(
                    effect_id="e1",
                    path="a.txt",
                    operation="write",
                    timestamp=ledger_timestamp(),
                    before_hash=None,
                    after_hash="h1",
                )
            )
            ledger.append(
                LedgerEntry(
                    effect_id="e2",
                    path="b.txt",
                    operation="delete",
                    timestamp=ledger_timestamp(),
                    before_hash="h1",
                    after_hash=None,
                )
            )
            entries = ledger.entries()
            self.assertEqual(2, len(entries))
            self.assertEqual("write", entries[0].operation)
            self.assertEqual("b.txt", ledger.last_entry_for("b.txt").path)
            self.assertIsNone(ledger.last_entry_for("missing.txt"))

    def test_ledger_run_id_roundtrip_and_legacy_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = FileMutationLedger(Path(directory) / "ledger.jsonl")
            ledger.append(
                LedgerEntry(
                    effect_id="e1",
                    path="a.txt",
                    operation="write",
                    timestamp=ledger_timestamp(),
                    before_hash=None,
                    after_hash="h1",
                    run_id="run_x",
                )
            )
            ledger.append(
                LedgerEntry(
                    effect_id="e2",
                    path="b.txt",
                    operation="write",
                    timestamp=ledger_timestamp(),
                    before_hash=None,
                    after_hash="h2",
                )
            )
            entries = ledger.entries()
            self.assertEqual("run_x", entries[0].run_id)
            self.assertIsNone(entries[1].run_id)  # legacy row parse
            self.assertIn('"run_id":"run_x"', ledger._path.read_text(encoding="utf-8"))
            # Legacy rows (pre-slice-D) never wrote the key; parsing keeps None.
            ledger._path.write_text('{"effect_id":"e3","path":"c.txt","operation":"write","timestamp":"t"}\n', encoding="utf-8")
            self.assertIsNone(ledger.entries()[0].run_id)


if __name__ == "__main__":
    unittest.main()
