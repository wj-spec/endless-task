"""M6 OE-1: bounded async trace recorder + retention policy."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_ledger.protocol import (
    RuntimeLedgerEvent,
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)
from endless_task.runtime_ledger.recorder import (
    AsyncTraceRecorder,
    TraceMode,
    parse_trace_mode,
)
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.storage import Database


def trace(run_id: str = "run_1", span_id: str | None = None) -> TraceContext:
    return TraceContext(
        trace_id="trace_1",
        run_id=run_id,
        correlation_id="corr_1",
        span_id=span_id,
    )


def event(event_id: str, *, safety: bool = False, event_type: str = "run.started") -> RuntimeLedgerEvent:
    return RuntimeLedgerEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at="2026-09-05T00:00:00Z",
        trace=trace(),
        safety_critical=safety,
    )


def span_spec(span_id: str) -> SpanSpec:
    return SpanSpec(
        trace=trace(span_id=span_id),
        kind=SpanKind.TOOL,
        name="tool.call",
        started_at="2026-09-05T00:00:00Z",
        monotonic_started=0.0,
    )


class ParseTraceModeTest(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertEqual(TraceMode.DISABLED, parse_trace_mode("0"))
        self.assertEqual(TraceMode.ERRORS, parse_trace_mode("errors"))
        self.assertEqual(TraceMode.SAMPLED, parse_trace_mode("sampled"))
        self.assertEqual(TraceMode.ALL, parse_trace_mode("all"))
        self.assertEqual(TraceMode.ALL, parse_trace_mode("1"))

    def test_illegal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_trace_mode("bogus")


class AsyncTraceRecorderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.sink = SqliteRuntimeLedger(self.database)

    async def asyncTearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_disabled_records_nothing(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.DISABLED)
        recorder.start()
        await recorder.append_event(event("evt_1"))
        await recorder.stop()
        self.assertEqual((), self.sink.events_for_run("run_1"))

    async def test_all_mode_flushes_events(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ALL)
        recorder.start()
        await recorder.append_event(event("evt_1"))
        await recorder.append_event(event("evt_2"))
        await recorder.stop()
        self.assertEqual(2, len(self.sink.events_for_run("run_1")))

    async def test_errors_mode_keeps_protected_drops_ordinary(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ERRORS)
        recorder.start()
        await recorder.append_event(event("evt_ord"))
        await recorder.append_event(
            event("evt_crit", safety=True, event_type="effect_receipt")
        )
        await recorder.stop()
        events = self.sink.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertEqual("evt_crit", events[0].event_id)

    async def test_failed_span_persisted_in_errors_mode(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ERRORS)
        recorder.start()
        handle = recorder.start_span(span_spec("span_fail"))
        await handle.end(
            SpanStatus.FAILED,
            ended_at="2026-09-05T00:00:01Z",
            monotonic_ended=1.0,
        )
        await recorder.stop()
        events = self.sink.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertEqual("span.failed", events[0].event_type)
        self.assertTrue(events[0].safety_critical)

    async def test_successful_span_dropped_in_errors_mode(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ERRORS)
        recorder.start()
        handle = recorder.start_span(span_spec("span_ok"))
        await handle.end(
            SpanStatus.COMPLETED,
            ended_at="2026-09-05T00:00:01Z",
            monotonic_ended=1.0,
        )
        await recorder.stop()
        self.assertEqual((), self.sink.events_for_run("run_1"))

    async def test_all_mode_keeps_successful_span(self) -> None:
        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ALL)
        recorder.start()
        handle = recorder.start_span(span_spec("span_ok"))
        await handle.end(
            SpanStatus.COMPLETED,
            ended_at="2026-09-05T00:00:01Z",
            monotonic_ended=1.0,
        )
        await recorder.stop()
        events = self.sink.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertEqual("span.completed", events[0].event_type)

    async def test_sampling_drops_some_spans(self) -> None:
        recorder = AsyncTraceRecorder(
            self.sink,
            mode=TraceMode.SAMPLED,
            sample_rate=0.5,
        )
        recorder.start()
        for index in range(6):
            handle = recorder.start_span(span_spec(f"span_{index}"))
            await handle.end(
                SpanStatus.COMPLETED,
                ended_at="2026-09-05T00:00:01Z",
                monotonic_ended=1.0,
            )
        await recorder.stop()
        events = self.sink.events_for_run("run_1")
        # Not all 6 survive sampling; failed spans always do.
        self.assertLess(len(events), 6)

    async def test_backpressure_drops_ordinary_but_blocks_protected(self) -> None:
        recorder = AsyncTraceRecorder(
            self.sink,
            mode=TraceMode.ALL,
            max_queue=2,
        )
        recorder.start()
        # Fill the queue with ordinary events without draining (stop the
        # loop first by not starting it is not possible; use a paused sink).
        # Instead: verify ordinary events beyond capacity are dropped while
        # protected ones wait - use a blocking sink.
        blocking = _BlockingSink(self.sink)
        recorder2 = AsyncTraceRecorder(blocking, mode=TraceMode.ALL, max_queue=1)
        recorder2.start()
        await recorder2.append_event(event("evt_a"))
        await recorder2.append_event(event("evt_b"))  # ordinary, queue full
        stats = recorder2.stats
        self.assertEqual(1, stats.queued)
        # Protected events must not be dropped - they block (run in task).
        await recorder2.append_event(
            event("evt_crit", safety=True, event_type="effect_receipt")
        )
        self.assertEqual(1, recorder2.stats.dropped_ordinary)
        blocking.release()
        await recorder2.stop()

    async def test_effect_records_synchronously(self) -> None:
        from endless_task.agent_platform import EffectOutcome, EffectReceipt

        recorder = AsyncTraceRecorder(self.sink, mode=TraceMode.ERRORS)
        recorder.start()
        receipt = EffectReceipt(
            effect_id="effect_1",
            tool_call_id="call_1",
            effect_type="workspace_write",
            target="notes.md",
            started_at="2026-09-05T00:00:00Z",
            outcome=EffectOutcome.COMMITTED,
            backend="local",
            safe_summary="已写入。",
            committed_at="2026-09-05T00:00:01Z",
        )
        await recorder.record_effect(receipt, trace())
        events = self.sink.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].safety_critical)
        await recorder.stop()


class _BlockingSink:
    """Sink that blocks append_event until released (backpressure test)."""

    def __init__(self, sink) -> None:
        self._sink = sink
        self._gate = asyncio.Event()

    async def append_event(self, event) -> None:
        await self._gate.wait()
        await self._sink.append_event(event)

    async def record_effect(self, receipt, trace) -> None:
        await self._sink.record_effect(receipt, trace)

    async def record_usage(self, usage) -> None:
        await self._sink.record_usage(usage)

    def start_span(self, spec):
        return self._sink.start_span(spec)

    def release(self) -> None:
        self._gate.set()


if __name__ == "__main__":
    unittest.main()
