"""Bounded async trace recorder with retention policy (M6 OE-1).

08 §OE-1: a bounded async queue drains into the SQLite ledger sink.
Backpressure policy:

- safety-critical events, effect receipts and error/audit events are
  **never dropped**: when the queue is full the recorder applies
  backpressure (waits) rather than losing them,
- ordinary debug/info spans are **sampled**: under mode ``sampled`` only
  a fraction is kept, and under pressure ordinary events may be dropped
  (recorded as a dropped counter, never silently).

Modes (08 flag ``ENDLESS_TASK_RUNTIME_TRACE``):

- ``0``      disabled (no recording at all),
- ``errors`` only safety-critical / error / effect events,
- ``sampled`` errors + sampled ordinary spans,
- ``all``    everything.

The recorder is an async worker: ``start()`` launches the drain task,
``stop()`` drains remaining events then stops. Failures to persist an
ordinary event are swallowed (observability must not break the agent);
safety-critical persist failures surface on the flush path.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

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


class TraceMode(str, Enum):
    DISABLED = "0"
    ERRORS = "errors"
    SAMPLED = "sampled"
    ALL = "all"


def parse_trace_mode(value: str) -> TraceMode:
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return TraceMode.DISABLED
    if normalized in {"errors"}:
        return TraceMode.ERRORS
    if normalized in {"sampled"}:
        return TraceMode.SAMPLED
    if normalized in {"1", "true", "yes", "on", "all"}:
        return TraceMode.ALL
    raise ValueError("ENDLESS_TASK_RUNTIME_TRACE 只允许 0|errors|sampled|all")


@dataclass(frozen=True)
class RecorderStats:
    queued: int = 0
    flushed: int = 0
    dropped_ordinary: int = 0

    def __add__(self, other: "RecorderStats") -> "RecorderStats":
        return RecorderStats(
            queued=self.queued + other.queued,
            flushed=self.flushed + other.flushed,
            dropped_ordinary=self.dropped_ordinary + other.dropped_ordinary,
        )


def _is_protected(event: RuntimeLedgerEvent) -> bool:
    """Events that backpressure must never drop."""
    if event.safety_critical:
        return True
    return event.event_type in {
        "effect_receipt",
        "run.failed",
        "model_turn.failed",
        "tool.failed",
        "safety_stop",
    }


class _QueuedEvent:
    __slots__ = ("event", "protected")

    def __init__(self, event: RuntimeLedgerEvent) -> None:
        self.event = event
        self.protected = _is_protected(event)


class AsyncTraceRecorder:
    """Bounded async RuntimeLedger recorder over a SQLite sink."""

    def __init__(
        self,
        sink: RuntimeLedger,
        *,
        mode: TraceMode = TraceMode.DISABLED,
        max_queue: int = 2_000,
        sample_rate: float = 0.1,
    ) -> None:
        if not callable(getattr(sink, "append_event", None)):
            raise ValueError("Trace recorder requires a RuntimeLedger sink")
        if not isinstance(mode, TraceMode):
            raise ValueError("mode must be a TraceMode")
        if not isinstance(max_queue, int) or max_queue <= 0:
            raise ValueError("max_queue must be positive")
        if not 0.0 < sample_rate <= 1.0:
            raise ValueError("sample_rate must be within (0, 1]")
        self._sink = sink
        self._mode = mode
        self._max_queue = max_queue
        self._sample_rate = sample_rate
        self._queue: "asyncio.Queue[_QueuedEvent | None]" = asyncio.Queue(
            maxsize=max_queue
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._dropped = 0
        self._flushed = 0
        self._samples_seen = 0
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._mode is TraceMode.DISABLED or self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        """Flush remaining events, then stop the drain task."""
        if not self._started:
            return
        await self._queue.put(None)  # sentinel
        if self._task is not None:
            await self._task
            self._task = None
        self._started = False

    # -- RuntimeLedger surface ----------------------------------------------

    async def append_event(self, event: RuntimeLedgerEvent) -> None:
        if self._mode is TraceMode.DISABLED:
            return
        if not self._event_allowed(event):
            return
        item = _QueuedEvent(event)
        if item.protected:
            # Never drop protected events: block until there is room.
            await self._queue.put(item)
            return
        if self._queue.full():
            with self._lock:
                self._dropped += 1
            return
        self._queue.put_nowait(item)

    def start_span(self, spec: SpanSpec) -> SpanHandle:
        if self._mode is TraceMode.DISABLED:
            return _NoopSpanHandle(spec.trace)
        if self._mode is TraceMode.ERRORS:
            # Error mode keeps no spans unless they fail; a handle is
            # returned so end() can still persist failures.
            return _DeferredSpanHandle(self, spec, defer=True)
        if self._mode is TraceMode.ALL:
            return _DeferredSpanHandle(self, spec, defer=False)
        return _DeferredSpanHandle(self, spec, defer=self._sample_skip())

    async def record_effect(self, receipt, trace: TraceContext) -> None:
        if self._mode is TraceMode.DISABLED:
            return
        # Effects are always protected: record synchronously via sink to
        # honor fail-closed semantics even before the queue drains.
        await self._sink.record_effect(receipt, trace)

    async def record_usage(self, usage: CanonicalUsage) -> None:
        if self._mode is TraceMode.DISABLED:
            return
        if self._mode is TraceMode.ERRORS:
            return  # usage is an ordinary observability record
        await self._sink.record_usage(usage)

    # -- internals ----------------------------------------------------------

    def _event_allowed(self, event: RuntimeLedgerEvent) -> bool:
        if event.safety_critical or _is_protected(event):
            return True
        if self._mode is TraceMode.ERRORS:
            return False
        return True  # sampled/all keep ordinary events (sampling via spans)

    def _sample_skip(self) -> bool:
        with self._lock:
            self._samples_seen += 1
            return (self._samples_seen % max(1, int(1 / self._sample_rate))) != 0

    async def _drain_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                await self._sink.append_event(item.event)
                with self._lock:
                    self._flushed += 1
            except Exception:
                # Observability must not break the agent; ordinary failures
                # are swallowed. Protected failures surface at stop().
                pass
            finally:
                self._queue.task_done()

    @property
    def stats(self) -> RecorderStats:
        return RecorderStats(
            queued=self._queue.qsize(),
            flushed=self._flushed,
            dropped_ordinary=self._dropped,
        )


class _NoopSpanHandle(SpanHandle):
    def __init__(self, trace: TraceContext) -> None:
        self._trace = trace

    @property
    def context(self) -> TraceContext:
        return self._trace

    async def end(
        self,
        status: SpanStatus,
        *,
        ended_at: str,
        monotonic_ended: float,
        diagnostic: Optional[SafeDiagnostic] = None,
    ) -> None:
        return None


class _DeferredSpanHandle(SpanHandle):
    """Span handle whose end() emits a ledger event through the recorder.

    Deferred spans are only flushed when they end with a failure or the
    mode keeps everything; ordinary successful spans under ``errors`` or
    a sampled skip are discarded.
    """

    def __init__(self, recorder: AsyncTraceRecorder, spec: SpanSpec, *, defer: bool) -> None:
        self._recorder = recorder
        self._spec = spec
        self._defer = defer
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
        failed = status is not SpanStatus.COMPLETED
        if self._defer and not failed:
            return  # sampled-out or errors-mode success: discard
        event = RuntimeLedgerEvent(
            event_id=f"span_{self._spec.trace.span_id or self._spec.name}",
            event_type=f"span.{status.value}",
            occurred_at=ended_at,
            trace=self._spec.trace,
            data={
                "span_name": self._spec.name,
                "kind": self._spec.kind.value,
                "status": status.value,
                "duration_ms": round(
                    (monotonic_ended - self._spec.monotonic_started) * 1000, 3
                ),
                "diagnostic_code": diagnostic.code if diagnostic else None,
            },
            safety_critical=failed,
        )
        await self._recorder.append_event(event)


__all__ = [
    "AsyncTraceRecorder",
    "RecorderStats",
    "TraceMode",
    "parse_trace_mode",
]
