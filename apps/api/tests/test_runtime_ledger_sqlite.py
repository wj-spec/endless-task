"""M6 OE-0: SQLite RuntimeLedger recorder persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import EffectOutcome, EffectReceipt, SafeDiagnostic
from endless_task.runtime_ledger.protocol import (
    CanonicalUsage,
    RuntimeLedgerEvent,
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
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


class SqliteRuntimeLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_persists_event_and_reads_back(self) -> None:
        import asyncio

        asyncio.run(
            self.ledger.append_event(
                RuntimeLedgerEvent(
                    event_id="evt_1",
                    event_type="run.started",
                    occurred_at="2026-09-05T00:00:00Z",
                    trace=trace(),
                    data={"run": "run_1"},
                )
            )
        )
        events = self.ledger.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertEqual("evt_1", events[0].event_id)
        self.assertEqual("run.started", events[0].event_type)
        self.assertEqual({"run": "run_1"}, events[0].data)
        self.assertFalse(events[0].safety_critical)

    def test_safety_critical_event_flag_persists(self) -> None:
        import asyncio

        asyncio.run(
            self.ledger.append_event(
                RuntimeLedgerEvent(
                    event_id="evt_crit",
                    event_type="effect.receipt",
                    occurred_at="2026-09-05T00:00:00Z",
                    trace=trace(),
                    data={"effect": "write"},
                    safety_critical=True,
                )
            )
        )
        event = self.ledger.events_for_run("run_1")[0]
        self.assertTrue(event.safety_critical)

    def test_span_end_persists_with_status_and_duration(self) -> None:
        import asyncio

        spec = SpanSpec(
            trace=trace(span_id="span_1"),
            kind=SpanKind.TOOL,
            name="tool.call",
            started_at="2026-09-05T00:00:00Z",
            monotonic_started=10.0,
            attributes={"tool_name": "read_file"},
        )
        handle = self.ledger.start_span(spec)
        asyncio.run(
            handle.end(
                SpanStatus.COMPLETED,
                ended_at="2026-09-05T00:00:01Z",
                monotonic_ended=10.5,
            )
        )
        spans = self.ledger.spans_for_run("run_1")
        self.assertEqual(1, len(spans))
        self.assertEqual("span_1", spans[0].span_id)
        self.assertEqual("tool.call", spans[0].name)
        self.assertEqual("completed", spans[0].status)
        self.assertEqual(500.0, spans[0].monotonic_duration_ms)
        self.assertEqual("read_file", spans[0].attributes["tool_name"])

    def test_span_end_persists_diagnostic(self) -> None:
        import asyncio

        spec = SpanSpec(
            trace=trace(span_id="span_2"),
            kind=SpanKind.MODEL,
            name="provider.call",
            started_at="2026-09-05T00:00:00Z",
            monotonic_started=0.0,
        )
        handle = self.ledger.start_span(spec)
        asyncio.run(
            handle.end(
                SpanStatus.FAILED,
                ended_at="2026-09-05T00:00:01Z",
                monotonic_ended=1.0,
                diagnostic=SafeDiagnostic(
                    code="provider_unavailable",
                    safe_message="模型服务不可用。",
                    retryable=True,
                ),
            )
        )
        span = self.ledger.spans_for_run("run_1")[0]
        self.assertEqual("failed", span.status)
        self.assertEqual("provider_unavailable", span.diagnostic_code)

    def test_end_twice_is_idempotent(self) -> None:
        import asyncio

        spec = SpanSpec(
            trace=trace(span_id="span_3"),
            kind=SpanKind.INTERNAL,
            name="run",
            started_at="2026-09-05T00:00:00Z",
            monotonic_started=0.0,
        )
        handle = self.ledger.start_span(spec)
        asyncio.run(
            handle.end(
                SpanStatus.COMPLETED,
                ended_at="2026-09-05T00:00:01Z",
                monotonic_ended=1.0,
            )
        )
        asyncio.run(
            handle.end(
                SpanStatus.FAILED,
                ended_at="2026-09-05T00:00:02Z",
                monotonic_ended=2.0,
            )
        )
        self.assertEqual(1, len(self.ledger.spans_for_run("run_1")))

    def test_record_usage_persists(self) -> None:
        import asyncio

        asyncio.run(
            self.ledger.record_usage(
                CanonicalUsage(
                    provider="deepseek",
                    model="deepseek-chat",
                    input_tokens=100,
                    output_tokens=50,
                    cached_input_tokens=10,
                    reasoning_tokens=5,
                    request_count=1,
                    occurred_at="2026-09-05T00:00:00Z",
                    trace=trace(),
                )
            )
        )
        usage = self.ledger.usage_for_run("run_1")
        self.assertEqual(1, len(usage))
        self.assertEqual("deepseek", usage[0].provider)
        self.assertEqual(100, usage[0].input_tokens)
        self.assertEqual(50, usage[0].output_tokens)
        self.assertEqual(10, usage[0].cached_input_tokens)

    def test_record_effect_persists_safety_critical_event(self) -> None:
        import asyncio

        receipt = EffectReceipt(
            effect_id="effect_1",
            tool_call_id="call_1",
            effect_type="workspace_write",
            target="notes.md",
            started_at="2026-09-05T00:00:00Z",
            outcome=EffectOutcome.COMMITTED,
            backend="local",
            safe_summary="已写入 notes.md。",
            committed_at="2026-09-05T00:00:01Z",
        )
        asyncio.run(self.ledger.record_effect(receipt, trace()))
        events = self.ledger.events_for_run("run_1")
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].safety_critical)
        self.assertEqual("effect_receipt", events[0].event_type)
        self.assertEqual("workspace_write", events[0].data["effect_type"])

    def test_record_usage_with_cost_persists_revision(self) -> None:
        import asyncio

        from endless_task.runtime_ledger.pricing import (
            UsageCategory,
            compute_cost,
            make_default_catalog,
        )

        usage = CanonicalUsage(
            provider="deepseek",
            model="deepseek-chat",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            reasoning_tokens=0,
            request_count=1,
            occurred_at="2026-09-05T00:00:00Z",
            trace=trace(),
        )
        cost = compute_cost(usage, make_default_catalog(), category=UsageCategory.COMPACTION)
        asyncio.run(self.ledger.record_usage(usage, cost=cost))
        stored = self.ledger.usage_for_run("run_1")[0]
        self.assertEqual(cost.price_revision, stored.price_revision)
        self.assertAlmostEqual(1.44, stored.cost_usd, places=6)
        self.assertEqual("compaction", stored.category)

    def test_reads_are_run_scoped(self) -> None:
        import asyncio

        asyncio.run(
            self.ledger.append_event(
                RuntimeLedgerEvent(
                    event_id="evt_a",
                    event_type="run.started",
                    occurred_at="2026-09-05T00:00:00Z",
                    trace=trace(run_id="run_a"),
                )
            )
        )
        asyncio.run(
            self.ledger.append_event(
                RuntimeLedgerEvent(
                    event_id="evt_b",
                    event_type="run.started",
                    occurred_at="2026-09-05T00:00:00Z",
                    trace=trace(run_id="run_b"),
                )
            )
        )
        self.assertEqual(1, len(self.ledger.events_for_run("run_a")))
        self.assertEqual(1, len(self.ledger.events_for_run("run_b")))


if __name__ == "__main__":
    unittest.main()
