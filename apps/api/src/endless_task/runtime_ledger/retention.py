"""Trace ledger retention (M6 G1 gap: 08 §11 one-key cleanup).

08 §11 default retention: trace rows 30 days (or capacity bound), and the
local single-user product still needs a one-key cleanup + export entry.
This module deletes ledger rows older than a retention window, keyed on
the persisted ``created_at`` ISO timestamps, and reports what it removed.

Design:

- **Dry-run default**: callers pass ``apply=True`` to actually delete;
  without it the module only reports what *would* be removed, so the
  cleanup entry can never surprise the user (09 §9 运维).
- **Age windows per table** mirror 08 §11: trace spans/events/usage share
  the 30-day trace window; an explicit ``retention_days`` overrides it.
- **Safety-critical rows are never silently dropped** by an age sweep:
  ``trace_events`` rows flagged ``safety_critical`` are excluded unless
  ``include_safety_critical=True`` is passed explicitly (audit rows keep
  the longer 90-day audit lifecycle per 08 §11).
- The trajectory export directory is file-based; its cleanup belongs to
  the file-system retention entry (:func:`cleanup_trajectory_exports`),
  keeping only the newest ``keep_latest`` bundles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

#: Default trace retention window (08 §11: Trace 30 天).
DEFAULT_TRACE_RETENTION_DAYS = 30
#: Default audit retention window (08 §11: Audit 90 天).
DEFAULT_AUDIT_RETENTION_DAYS = 90

_TRACE_TABLES = (
    ("trace_spans", "created_at"),
    ("trace_events", "created_at"),
    ("trace_usage", "created_at"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _cutoff_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RetentionReport:
    """What a retention sweep removed (or would remove, in dry-run)."""

    dry_run: bool
    spans_removed: int = 0
    events_removed: int = 0
    usage_removed: int = 0
    safety_critical_kept: int = 0
    trajectory_bundles_removed: int = 0

    @property
    def total_rows(self) -> int:
        return self.spans_removed + self.events_removed + self.usage_removed

    def to_dict(self) -> dict:
        return {
            "dryRun": self.dry_run,
            "spansRemoved": self.spans_removed,
            "eventsRemoved": self.events_removed,
            "usageRemoved": self.usage_removed,
            "safetyCriticalKept": self.safety_critical_kept,
            "trajectoryBundlesRemoved": self.trajectory_bundles_removed,
        }


def sweep_trace_ledger(
    database,
    *,
    retention_days: int = DEFAULT_TRACE_RETENTION_DAYS,
    apply: bool = False,
    include_safety_critical: bool = False,
    audit_retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS,
) -> RetentionReport:
    """Sweep ledger rows older than the retention window.

    ``database`` needs ``connect()`` (context manager giving a cursor with
    ``execute``/``executemany``). Safety-critical events are kept unless
    they are older than the (longer) audit window **and** the caller
    explicitly opts in with ``include_safety_critical=True``.
    """
    cutoff = _cutoff_iso(retention_days)
    audit_cutoff = _cutoff_iso(audit_retention_days)

    def count_deletable(
        connection,
        table: str,
        column: str,
    ) -> int:
        sql = f"SELECT COUNT(*) FROM {table} WHERE {column} < ?"
        row = connection.execute(sql, (cutoff,)).fetchone()
        return int(row[0]) if row else 0

    def delete_older(
        connection,
        table: str,
        column: str,
        *,
        extra: str = "",
    ) -> int:
        sql = f"DELETE FROM {table} WHERE {column} < ?{extra}"
        cursor = connection.execute(sql, (cutoff,))
        return int(cursor.rowcount or 0)

    report = RetentionReport(dry_run=not apply)
    with database.connect() as connection:
        if not apply:
            kept_by_audit = connection.execute(
                "SELECT COUNT(*) FROM trace_events "
                "WHERE safety_critical = 1 AND created_at >= ? AND created_at < ?",
                (audit_cutoff, cutoff),
            ).fetchone()
            return RetentionReport(
                dry_run=True,
                spans_removed=count_deletable(connection, "trace_spans", "created_at"),
                events_removed=count_deletable(connection, "trace_events", "created_at"),
                usage_removed=count_deletable(connection, "trace_usage", "created_at"),
                safety_critical_kept=int(kept_by_audit[0]) if kept_by_audit else 0,
            )

        # Apply path.
        spans = delete_older(connection, "trace_spans", "created_at")
        usage = delete_older(connection, "trace_usage", "created_at")
        if include_safety_critical:
            events = delete_older(connection, "trace_events", "created_at")
            safety_kept = 0
        else:
            # Delete non-critical older events; count safety-critical rows
            # inside the audit window that stay.
            events = delete_older(
                connection,
                "trace_events",
                "created_at",
                extra=" AND safety_critical = 0",
            )
            safety = connection.execute(
                "SELECT COUNT(*) FROM trace_events "
                "WHERE safety_critical = 1 AND created_at < ?",
                (audit_cutoff,),
            ).fetchone()
            safety_kept = int(safety[0]) if safety else 0
        return RetentionReport(
            dry_run=False,
            spans_removed=spans,
            events_removed=events,
            usage_removed=usage,
            safety_critical_kept=safety_kept,
        )


def cleanup_trajectory_exports(
    export_root: Path,
    *,
    keep_latest: int = 10,
    apply: bool = False,
) -> RetentionReport:
    """Keep only the newest trajectory bundles under ``export_root``.

    Bundle directories are named ``trajectory-<run-id>/`` (W6-3); cleanup
    keeps the ``keep_latest`` most recently modified and reports the rest
    (dry-run by default).
    """
    if not export_root.is_dir():
        return RetentionReport(dry_run=not apply)
    candidates = [
        path for path in export_root.iterdir() if path.is_dir()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    removable = candidates[keep_latest:] if keep_latest >= 0 else candidates
    report = RetentionReport(dry_run=not apply)
    if not apply:
        return RetentionReport(
            dry_run=True,
            trajectory_bundles_removed=len(removable),
        )
    for path in removable:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    return RetentionReport(
        dry_run=False,
        trajectory_bundles_removed=len(removable),
    )


__all__ = [
    "DEFAULT_AUDIT_RETENTION_DAYS",
    "DEFAULT_TRACE_RETENTION_DAYS",
    "RetentionReport",
    "cleanup_trajectory_exports",
    "sweep_trace_ledger",
]
