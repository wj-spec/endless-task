"""M6 G1 gap: trace ledger retention sweep + trajectory cleanup."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.runtime_ledger.protocol import (
    RuntimeLedgerEvent,
    TraceContext,
)
from endless_task.runtime_ledger.retention import (
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_TRACE_RETENTION_DAYS,
    cleanup_trajectory_exports,
    sweep_trace_ledger,
)
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.storage import Database


def _iso(days_ago: int) -> str:
    instant = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _trace(run_id: str) -> TraceContext:
    return TraceContext(
        trace_id=f"trace_{run_id}",
        run_id=run_id,
        correlation_id="corr_1",
    )


class RetentionSweepTest(unittest.TestCase):
    def setUp(self) -> None:
        import asyncio

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)

        # Seed rows with backdated created_at timestamps by inserting
        # directly (the ledger uses the wall clock, so we bypass it).
        def insert(table: str, row: dict) -> None:
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            with self.database.transaction() as connection:
                connection.execute(
                    f"INSERT INTO {table}({columns}) VALUES ({placeholders})",
                    tuple(row.values()),
                )

        insert(
            "trace_spans",
            {
                "span_id": "span_old",
                "trace_id": "trace_old",
                "run_id": "run_old",
                "correlation_id": "corr_1",
                "kind": "internal",
                "name": "old_span",
                "status": "completed",
                "started_at": _iso(45),
                "attributes_json": "{}",
                "created_at": _iso(45),
            },
        )
        insert(
            "trace_spans",
            {
                "span_id": "span_new",
                "trace_id": "trace_new",
                "run_id": "run_new",
                "correlation_id": "corr_1",
                "kind": "internal",
                "name": "new_span",
                "status": "completed",
                "started_at": _iso(1),
                "attributes_json": "{}",
                "created_at": _iso(1),
            },
        )
        insert(
            "trace_events",
            {
                "event_id": "evt_old",
                "trace_id": "trace_old",
                "run_id": "run_old",
                "correlation_id": "corr_1",
                "event_type": "run.started",
                "safety_critical": 0,
                "occurred_at": _iso(45),
                "data_json": "{}",
                "created_at": _iso(45),
            },
        )
        insert(
            "trace_events",
            {
                "event_id": "evt_crit_old",
                "trace_id": "trace_old",
                "run_id": "run_old",
                "correlation_id": "corr_1",
                "event_type": "effect_receipt",
                "safety_critical": 1,
                "occurred_at": _iso(45),
                "data_json": "{}",
                "created_at": _iso(45),
            },
        )
        insert(
            "trace_usage",
            {
                "trace_id": "trace_old",
                "run_id": "run_old",
                "correlation_id": "corr_1",
                "provider": "p",
                "model": "m",
                "input_tokens": 1,
                "output_tokens": 1,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "request_count": 1,
                "occurred_at": _iso(45),
                "created_at": _iso(45),
            },
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_dry_run_reports_without_deleting(self) -> None:
        report = sweep_trace_ledger(self.database, retention_days=30)
        self.assertTrue(report.dry_run)
        self.assertEqual(1, report.spans_removed)  # old span only
        self.assertEqual(2, report.events_removed)  # old evt + crit evt
        self.assertEqual(1, report.usage_removed)
        self.assertGreaterEqual(report.safety_critical_kept, 1)
        # Nothing actually deleted.
        with self.database.connect() as connection:
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM trace_spans"
                ).fetchone()[0],
            )

    def test_apply_deletes_old_rows_keeps_new(self) -> None:
        report = sweep_trace_ledger(
            self.database,
            retention_days=30,
            apply=True,
        )
        self.assertFalse(report.dry_run)
        with self.database.connect() as connection:
            spans = connection.execute(
                "SELECT span_id FROM trace_spans"
            ).fetchall()
            events = connection.execute(
                "SELECT event_id, safety_critical FROM trace_events"
            ).fetchall()
            usage = connection.execute(
                "SELECT COUNT(*) FROM trace_usage"
            ).fetchone()[0]
        self.assertEqual(["span_new"], [row["span_id"] for row in spans])
        self.assertEqual(0, usage)
        # Safety-critical event survived the default sweep (audit window).
        event_ids = [row["event_id"] for row in events]
        self.assertIn("evt_crit_old", event_ids)
        self.assertNotIn("evt_old", event_ids)

    def test_include_safety_critical_removes_audit_rows(self) -> None:
        sweep_trace_ledger(
            self.database,
            retention_days=30,
            audit_retention_days=1,
            apply=True,
            include_safety_critical=True,
        )
        with self.database.connect() as connection:
            events = connection.execute(
                "SELECT COUNT(*) FROM trace_events"
            ).fetchone()[0]
        self.assertEqual(0, events)


class TrajectoryCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name) / "exports"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_dry_run_reports_without_removing(self) -> None:
        for index in range(3):
            (self.root / f"trajectory-run_{index}").mkdir()
        report = cleanup_trajectory_exports(self.root, keep_latest=1)
        self.assertTrue(report.dry_run)
        self.assertEqual(2, report.trajectory_bundles_removed)
        self.assertEqual(3, len(list(self.root.iterdir())))

    def test_apply_keeps_latest(self) -> None:
        for index in range(3):
            (self.root / f"trajectory-run_{index}").mkdir()
        report = cleanup_trajectory_exports(
            self.root, keep_latest=2, apply=True
        )
        self.assertFalse(report.dry_run)
        self.assertEqual(1, report.trajectory_bundles_removed)
        self.assertEqual(2, len(list(self.root.iterdir())))

    def test_missing_root_is_noop(self) -> None:
        report = cleanup_trajectory_exports(
            Path(self._temporary_directory.name) / "nope",
            apply=True,
        )
        self.assertEqual(0, report.trajectory_bundles_removed)


if __name__ == "__main__":
    unittest.main()
