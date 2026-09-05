"""Failed-run trajectory export from the v2 journal (M6 W6-3).

08 §OE-3 / §11: trajectory bundles exist for **failed runs and explicit
requests** (never for every success), and export must pass the redaction
pipeline. The v2 journal is the source of truth; this exporter reads a
terminal run's journal records and writes an OE-3 bundle directory
(08 §8 layout) under ``v2_trajectory_exports/<run-id>/``.

Design:

- Only terminal **failed** runs export automatically (volume bound,
  08 §11); completed runs export only via explicit request
  (:meth:`export_run` is callable for any run).
- Journal payloads may carry model text deltas / tool arguments, so every
  record passes the OE-3 redaction machinery (trajectory.py): secret
  patterns scrubbed, raw payload keys dropped. The bundle layout is
  written with :func:`write_trajectory_bundle` so downstream replay and
  eval treat it identically to ledger-exported bundles.
- Export is fail-open: errors are logged and never break the run; the
  journal remains authoritative (08 §OE-5 philosophy).

Import discipline: journal accessors are injected callables (duck-typed,
no storage import at module top).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalEventView:
    """One journal runtime event reduced to exportable facts."""

    event_id: str
    event_type: str
    occurred_at: str
    safety_critical: bool
    data: dict


@dataclass(frozen=True)
class JournalRunView:
    """Terminal run facts needed to build one trajectory bundle."""

    run_id: str
    status: str
    error_code: Optional[str]
    safe_message: Optional[str]
    started_at: str
    finished_at: Optional[str]
    correlation_id: str
    events: Sequence[JournalEventView]


def default_journal_reader(repository) -> Callable[[str], JournalRunView]:
    """Read a run + its journal events from a v2 repository (duck-typed).

    ``repository`` needs ``get_run`` and ``list_runtime_events``. Event
    payloads are carried as-is; redaction happens at export time.
    """

    def read(run_id: str) -> JournalRunView:
        run = repository.get_run(run_id)
        events: list[JournalEventView] = []
        for event in repository.list_runtime_events(run_id):
            events.append(
                JournalEventView(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    safety_critical=event.event_type
                    in {
                        "run_failed",
                        "run_cancelled",
                        "safety_stop",
                        "model_turn_failed",
                        "tool_execution_rejected",
                    },
                    data=dict(event.payload),
                )
            )
        return JournalRunView(
            run_id=run.id,
            status=run.status.value,
            error_code=run.error_code,
            safe_message=run.safe_message,
            started_at=run.started_at,
            finished_at=run.finished_at,
            correlation_id=run.correlation_id or run.id,
            events=tuple(events),
        )

    return read


class RunTrajectoryExporter:
    """Export terminal runs to OE-3 trajectory bundle directories."""

    def __init__(
        self,
        *,
        export_root,
        journal_reader: Callable[[str], JournalRunView],
        runtime_version: str,
        provider: str,
        model: str,
        config_fingerprint: str,
        usage_ledger=None,
    ) -> None:
        if not callable(journal_reader):
            raise ValueError("journal_reader must be callable")
        if usage_ledger is not None and not callable(
            getattr(usage_ledger, "usage_for_run", None)
        ):
            raise ValueError("usage_ledger must expose usage_for_run")
        self._export_root = export_root
        self._journal_reader = journal_reader
        self._runtime_version = runtime_version
        self._provider = provider
        self._model = model
        self._config_fingerprint = config_fingerprint
        self._usage_ledger = usage_ledger

    def export_failed_run(self, run_id: str) -> Optional[str]:
        """Auto-export path: only terminal failed runs export a directory.

        Returns the bundle directory on success, ``None`` when skipped
        (non-failed or error). Never raises.
        """
        try:
            view = self._journal_reader(run_id)
            if view.status != "failed":
                logger.info(
                    "Trajectory auto-export only applies to failed runs",
                    extra={"run_id": run_id, "status": view.status},
                )
                return None
            return self._write_bundle(view)
        except Exception:
            logger.exception(
                "Failed to auto-export trajectory for run",
                extra={"run_id": run_id},
            )
            return None

    async def on_run_terminal(self, run_id: str) -> None:
        """Executor seam: auto-export failed runs (observer protocol)."""
        self.export_failed_run(run_id)

    def export_run(self, run_id: str) -> Optional[str]:
        """Explicit export path: any terminal run may be requested."""
        try:
            return self._write_bundle(self._journal_reader(run_id))
        except Exception:
            logger.exception(
                "Failed to export trajectory for run",
                extra={"run_id": run_id},
            )
            return None

    def _write_bundle(self, view: JournalRunView) -> str:
        # Imported by path (not through runtime_ledger/__init__): keeps
        # this module free of storage imports at module top.
        from endless_task.runtime_ledger.trajectory import (
            TrajectoryBundle,
            TrajectoryManifest,
            redact_record,
            write_trajectory_bundle,
        )

        manifest = TrajectoryManifest(
            schema_version=1,
            runtime_version=self._runtime_version,
            provider=self._provider,
            model=self._model,
            config_fingerprint=self._config_fingerprint,
            redaction_policy_revision="r1",
            source_run_id=view.run_id,
        )
        events = tuple(
            redact_record(
                {
                    "eventId": event.event_id,
                    "eventType": event.event_type,
                    "occurredAt": event.occurred_at,
                    "traceId": view.run_id,
                    "runId": view.run_id,
                    "safetyCritical": event.safety_critical,
                    "data": event.data,
                }
            )
            for event in view.events
        )
        expected = redact_record(
            {
                "terminal": view.status,
                "errorCode": view.error_code,
                "safeMessage": view.safe_message,
                "startedAt": view.started_at,
                "finishedAt": view.finished_at,
            }
        )
        usages = ()
        if self._usage_ledger is not None:
            usages = tuple(
                {
                    "provider": row.provider,
                    "model": row.model,
                    "inputTokens": row.input_tokens,
                    "outputTokens": row.output_tokens,
                    "requestCount": row.request_count,
                    "costUsd": row.cost_usd,
                }
                for row in self._usage_ledger.usage_for_run(view.run_id)
            )
        # Every record passes the same redaction pipeline as ledger
        # exports (08 §8), so journal payload secrets never reach disk.
        bundle = TrajectoryBundle(
            manifest=manifest,
            events=events,
            usages=usages,
            expected=expected,
        )
        directory = self._export_root / f"trajectory-{view.run_id}"
        write_trajectory_bundle(bundle, directory)
        logger.info(
            "Exported trajectory bundle",
            extra={"run_id": view.run_id, "directory": str(directory)},
        )
        return str(directory)


class FanoutTraceObserver:
    """Terminal-run fan-out so one executor seam drives several observers.

    Used by the composition root to run the usage mirror (W6-1) and the
    failed-run trajectory exporter (W6-3) from the single executor hook.
    Fail-open: one observer failing never blocks the others or the run.
    """

    def __init__(self, observers: Sequence) -> None:
        if not observers:
            raise ValueError("FanoutTraceObserver requires at least one observer")
        for observer in observers:
            if not callable(getattr(observer, "on_run_terminal", None)):
                raise ValueError("each observer must expose on_run_terminal")
        self._observers = tuple(observers)

    async def on_run_terminal(self, run_id: str) -> None:
        for observer in self._observers:
            try:
                await observer.on_run_terminal(run_id)
            except Exception:
                logger.exception(
                    "Fanout trace observer failed for run",
                    extra={"run_id": run_id, "observer": type(observer).__name__},
                )


__all__ = [
    "FanoutTraceObserver",
    "JournalEventView",
    "JournalRunView",
    "RunTrajectoryExporter",
    "JournalOtelBridge",
    "default_journal_reader",
]


#: Journal event types that project to OTLP log records. Content-bearing
#: events (model_text_delta, tool_progress_update, context_fingerprint,
#: steer_injected) are excluded by default (08 §2.2: no secrets/content in
#: observability by default).
_OTEL_EXPORTABLE_EVENT_TYPES = frozenset(
    {
        "run_cancelled",
        "run_failed",
        "run_status_changed",
        "run_cancel_requested",
        "safety_stop",
        "model_turn_cancelled",
        "model_turn_failed",
        "tool_execution_rejected",
        "compaction_started",
        "compaction_completed",
    }
)

#: Payload keys projected onto OTLP log records; anything else is dropped
#: before export (second gate after EVENT_DATA_ALLOWLIST).
_OTEL_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "errorCode",
        "safeMessage",
        "reason",
        "status",
        "toolName",
        "cancelledBy",
        "releasedTokens",
    }
)


class JournalOtelBridge:
    """Project exportable journal events to OTLP via :class:`OtelExporter`.

    The v2 journal is the source of truth; OTel stays an exporter (08
    §OE-5). Only allowlisted event types and payload keys are projected;
    the exporter applies its own attribute allowlist as a second gate, so
    secrets cannot leak through either layer. Export failures return
    ``False`` from ``export_batch`` and never break the run.
    """

    def __init__(
        self,
        *,
        exporter,
        journal_reader: Callable[[str], JournalRunView],
    ) -> None:
        if not callable(journal_reader):
            raise ValueError("journal_reader must be callable")
        if not callable(getattr(exporter, "export_batch", None)):
            raise ValueError("exporter must expose export_batch")
        self._exporter = exporter
        self._journal_reader = journal_reader

    async def on_run_terminal(self, run_id: str) -> None:
        try:
            view = self._journal_reader(run_id)
            events = tuple(
                self._project_event(view, event)
                for event in view.events
                if event.event_type in _OTEL_EXPORTABLE_EVENT_TYPES
            )
            if not events:
                return
            self._exporter.export_batch(events=events)
        except Exception:
            logger.exception(
                "OTel journal bridge failed for run",
                extra={"run_id": run_id},
            )

    def _project_event(
        self,
        view: JournalRunView,
        event: JournalEventView,
    ):
        # Imported by path: keeps this module free of storage imports at
        # module top.
        from endless_task.runtime_ledger.sqlite_recorder import StoredLedgerEvent

        data = {
            key: event.data[key]
            for key in _OTEL_SAFE_PAYLOAD_KEYS
            if key in event.data
        }
        return StoredLedgerEvent(
            event_id=event.event_id,
            trace_id=view.run_id,
            run_id=view.run_id,
            correlation_id=view.correlation_id,
            event_type=event.event_type,
            safety_critical=event.safety_critical,
            occurred_at=event.occurred_at,
            data=data,
        )
