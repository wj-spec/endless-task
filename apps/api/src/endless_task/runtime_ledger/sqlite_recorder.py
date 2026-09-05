"""SQLite-backed RuntimeLedger recorder (M6 OE-0).

Implements the frozen :class:`RuntimeLedger` protocol with a real SQLite
sink (migration 055) so traces/spans/usage persist across restarts:

- ``start_span`` returns an in-memory handle; ``end`` flushes the span
  row with status/duration/diagnostic,
- ``append_event`` persists each journal event (safety-critical events
  are persisted synchronously; ordinary events share the same sink for
  OE-0, with a bounded async queue landing in OE-1),
- ``record_usage`` persists one canonical usage row per request,
- read-side queries (``spans_for_run`` / ``events_for_run`` /
  ``usage_for_run``) back OE-3 trajectory export and OE-4 eval later.

The protocol objects only ever carry allowlisted trace attributes, so no
secrets reach these tables by construction (08 §2.2). Writes go through
the shared Database transaction helper; a failure to persist an ordinary
observation raises (callers choose whether to swallow), while the ledger
protocol's fail-closed semantics for safety-critical data are honored by
writing them in the same transaction as the caller's effect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from endless_task.agent_platform import SafeDiagnostic
from endless_task.runtime_ledger.protocol import (
    CanonicalUsage,
    RuntimeLedger,
    RuntimeLedgerEvent,
    SpanHandle,
    SpanSpec,
    SpanStatus,
    TraceContext,
)

from ..storage.database import Database


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class StoredSpan:
    span_id: str
    trace_id: str
    run_id: str
    correlation_id: str
    parent_span_id: Optional[str]
    kind: str
    name: str
    status: str
    started_at: str
    ended_at: Optional[str]
    monotonic_duration_ms: Optional[float]
    attributes: Mapping[str, Any]
    diagnostic_code: Optional[str]
    diagnostic_message: Optional[str]


@dataclass(frozen=True)
class StoredLedgerEvent:
    event_id: str
    trace_id: str
    run_id: str
    correlation_id: str
    event_type: str
    safety_critical: bool
    occurred_at: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class StoredUsage:
    usage_id: int
    trace_id: str
    run_id: str
    correlation_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    request_count: int
    occurred_at: str


class _SpanHandleImpl(SpanHandle):
    def __init__(self, recorder: "SqliteRuntimeLedger", spec: SpanSpec) -> None:
        self._recorder = recorder
        self._spec = spec
        self._ended = False

    @property
    def context(self) -> TraceContext:
        return self._spec.trace

    async def end(
        self,
        status: SpanStatus,
        *,
        ended_at: str,
        monotonic_ended: float,
        diagnostic: Optional[SafeDiagnostic] = None,
    ) -> None:
        if self._ended:
            return
        self._ended = True
        self._recorder._persist_span(
            self._spec,
            status=status,
            ended_at=ended_at,
            monotonic_ended=monotonic_ended,
            diagnostic=diagnostic,
        )


class SqliteRuntimeLedger:
    """RuntimeLedger implementation persisting to the trace tables."""

    def __init__(
        self,
        database: Database,
        *,
        clock=None,
    ) -> None:
        self._database = database
        self._clock = clock or _utc_now

    # -- RuntimeLedger ------------------------------------------------------

    async def append_event(self, event: RuntimeLedgerEvent) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO trace_events(
                    event_id, trace_id, run_id, correlation_id, event_type,
                    safety_critical, occurred_at, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trace.trace_id,
                    event.trace.run_id,
                    event.trace.correlation_id,
                    event.event_type,
                    1 if event.safety_critical else 0,
                    event.occurred_at,
                    _dump(dict(event.data)),
                    self._clock(),
                ),
            )

    def start_span(self, spec: SpanSpec) -> SpanHandle:
        return _SpanHandleImpl(self, spec)

    async def record_effect(self, receipt, trace: TraceContext) -> None:
        # OE-0 persists every effect receipt as a safety-critical ledger
        # event (a dedicated effects table/retention lands in OE-1). The
        # ledger protocol's fail-closed semantics: a receipt that cannot be
        # persisted raises so the caller does not report a safe outcome for
        # an unrecorded side effect.
        data: Mapping[str, Any] = {
            "effect_id": receipt.effect_id,
            "tool_call_id": receipt.tool_call_id,
            "effect_type": receipt.effect_type,
            "outcome": (
                receipt.outcome.value
                if hasattr(receipt.outcome, "value")
                else str(receipt.outcome)
            ),
            "backend": receipt.backend,
            "safe_summary": receipt.safe_summary,
        }
        await self.append_event(
            RuntimeLedgerEvent(
                event_id=f"effect_{receipt.effect_id}",
                event_type="effect_receipt",
                occurred_at=receipt.committed_at or receipt.started_at,
                trace=trace,
                data=data,
                safety_critical=True,
            )
        )

    async def record_usage(self, usage: CanonicalUsage) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO trace_usage(
                    trace_id, run_id, correlation_id, provider, model,
                    input_tokens, output_tokens, cached_input_tokens,
                    reasoning_tokens, request_count, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.trace.trace_id,
                    usage.trace.run_id,
                    usage.trace.correlation_id,
                    usage.provider,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_input_tokens,
                    usage.reasoning_tokens,
                    usage.request_count,
                    usage.occurred_at,
                    self._clock(),
                ),
            )

    # -- persistence helper (sync flush) ------------------------------------

    def _persist_span(
        self,
        spec: SpanSpec,
        *,
        status: SpanStatus,
        ended_at: str,
        monotonic_ended: float,
        diagnostic: Optional[SafeDiagnostic],
    ) -> None:
        duration_ms = None
        if spec.monotonic_started is not None and monotonic_ended is not None:
            duration_ms = round((monotonic_ended - spec.monotonic_started) * 1000, 3)
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans(
                    span_id, trace_id, run_id, correlation_id, parent_span_id,
                    kind, name, status, started_at, ended_at,
                    monotonic_duration_ms, attributes_json,
                    diagnostic_code, diagnostic_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.trace.span_id or spec.name,
                    spec.trace.trace_id,
                    spec.trace.run_id,
                    spec.trace.correlation_id,
                    spec.trace.parent_span_id,
                    spec.kind.value,
                    spec.name,
                    status.value,
                    spec.started_at,
                    ended_at,
                    duration_ms,
                    _dump(dict(spec.attributes)),
                    diagnostic.code if diagnostic is not None else None,
                    diagnostic.safe_message if diagnostic is not None else None,
                    self._clock(),
                ),
            )

    # -- read side (OE-3 trajectory / OE-4 eval) ----------------------------

    def spans_for_run(self, run_id: str) -> Tuple[StoredSpan, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT span_id, trace_id, run_id, correlation_id, parent_span_id,
                       kind, name, status, started_at, ended_at,
                       monotonic_duration_ms, attributes_json,
                       diagnostic_code, diagnostic_message
                FROM trace_spans
                WHERE run_id = ?
                ORDER BY started_at
                """,
                (run_id,),
            ).fetchall()
        return tuple(_span_from_row(row) for row in rows)

    def events_for_run(self, run_id: str) -> Tuple[StoredLedgerEvent, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, trace_id, run_id, correlation_id, event_type,
                       safety_critical, occurred_at, data_json
                FROM trace_events
                WHERE run_id = ?
                ORDER BY occurred_at
                """,
                (run_id,),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def usage_for_run(self, run_id: str) -> Tuple[StoredUsage, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT usage_id, trace_id, run_id, correlation_id, provider,
                       model, input_tokens, output_tokens, cached_input_tokens,
                       reasoning_tokens, request_count, occurred_at
                FROM trace_usage
                WHERE run_id = ?
                ORDER BY occurred_at
                """,
                (run_id,),
            ).fetchall()
        return tuple(_usage_from_row(row) for row in rows)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _span_from_row(row) -> StoredSpan:
    return StoredSpan(
        span_id=row["span_id"],
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        correlation_id=row["correlation_id"],
        parent_span_id=row["parent_span_id"],
        kind=row["kind"],
        name=row["name"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        monotonic_duration_ms=row["monotonic_duration_ms"],
        attributes=_load(row["attributes_json"]),
        diagnostic_code=row["diagnostic_code"],
        diagnostic_message=row["diagnostic_message"],
    )


def _event_from_row(row) -> StoredLedgerEvent:
    return StoredLedgerEvent(
        event_id=row["event_id"],
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        correlation_id=row["correlation_id"],
        event_type=row["event_type"],
        safety_critical=bool(row["safety_critical"]),
        occurred_at=row["occurred_at"],
        data=_load(row["data_json"]),
    )


def _usage_from_row(row) -> StoredUsage:
    return StoredUsage(
        usage_id=row["usage_id"],
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        correlation_id=row["correlation_id"],
        provider=row["provider"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cached_input_tokens=row["cached_input_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        request_count=row["request_count"],
        occurred_at=row["occurred_at"],
    )


__all__ = [
    "SqliteRuntimeLedger",
    "StoredLedgerEvent",
    "StoredSpan",
    "StoredUsage",
]
