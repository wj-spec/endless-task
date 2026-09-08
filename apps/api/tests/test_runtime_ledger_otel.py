"""M6 OE-5: optional OTLP/HTTP trace exporter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import SafeDiagnostic
from endless_task.runtime_ledger.otel_exporter import (
    EVENT_DATA_ALLOWLIST,
    OtelExportMode,
    OtelExporter,
    OtelExporterConfig,
    parse_otel_export_mode,
)
from endless_task.runtime_ledger.protocol import (
    RuntimeLedgerEvent,
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)
from endless_task.runtime_ledger.sqlite_recorder import (
    SqliteRuntimeLedger,
    StoredLedgerEvent,
    StoredSpan,
)
from endless_task.storage import Database


def trace(run_id: str = "run_1") -> TraceContext:
    return TraceContext(
        trace_id="trace_1",
        run_id=run_id,
        correlation_id="corr_1",
    )


def _config(
    mode: OtelExportMode = OtelExportMode.OTLP_HTTP,
) -> OtelExporterConfig:
    return OtelExporterConfig(mode=mode)


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.fail = False

    def __call__(self, endpoint: str, body: bytes) -> int:
        if self.fail:
            raise ConnectionError("collector unreachable")
        self.calls.append((endpoint, body))
        return 200


class ModeParsingTest(unittest.TestCase):
    def test_off_forms(self) -> None:
        for value in ("0", "off", "false", "disabled", ""):
            self.assertIs(
                OtelExportMode.DISABLED,
                parse_otel_export_mode(value),
                value,
            )

    def test_on_forms(self) -> None:
        for value in ("1", "otlp-http", "otlp", "true", "on"):
            self.assertIs(
                OtelExportMode.OTLP_HTTP,
                parse_otel_export_mode(value),
                value,
            )

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_otel_export_mode("prometheus")

    def test_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            OtelExporterConfig(endpoint="not-a-url")
        with self.assertRaises(ValueError):
            OtelExporterConfig(service_name="  ")


def _stored_span(**overrides) -> StoredSpan:
    fields = dict(
        span_id="span_1",
        trace_id="trace_1",
        run_id="run_1",
        correlation_id="corr_1",
        parent_span_id=None,
        kind="model",
        name="provider.call",
        status="completed",
        started_at="2026-09-05T00:00:00Z",
        ended_at="2026-09-05T00:00:01Z",
        monotonic_duration_ms=1000.0,
        attributes={"provider": "deepseek", "model": "deepseek-chat"},
        diagnostic_code=None,
        diagnostic_message=None,
    )
    fields.update(overrides)
    return StoredSpan(**fields)


def _stored_event(**overrides) -> StoredLedgerEvent:
    fields = dict(
        event_id="evt_1",
        trace_id="trace_1",
        run_id="run_1",
        correlation_id="corr_1",
        event_type="effect_receipt",
        safety_critical=True,
        occurred_at="2026-09-05T00:00:01Z",
        data={"effect_id": "fx_1", "outcome": "committed"},
    )
    fields.update(overrides)
    return StoredLedgerEvent(**fields)


class PayloadBuilderTest(unittest.TestCase):
    def test_span_maps_to_otlp_span(self) -> None:
        exporter = OtelExporter(_config())
        payload = exporter.build_payload(spans=[_stored_span()])
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(1, len(spans))
        otlp = spans[0]
        self.assertEqual("provider.call", otlp["name"])
        self.assertEqual(3, otlp["kind"])  # client
        self.assertEqual(1, otlp["status"]["code"])  # OK
        self.assertIn("traceId", otlp)
        self.assertIn("spanId", otlp)
        attrs = {item["key"]: item["value"] for item in otlp["attributes"]}
        self.assertEqual("deepseek", attrs["provider"]["stringValue"])

    def test_event_maps_to_log_record(self) -> None:
        exporter = OtelExporter(_config())
        payload = exporter.build_payload(events=[_stored_event()])
        logs = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(1, len(logs))
        record = logs[0]
        self.assertEqual("WARN", record["severityText"])
        self.assertEqual("effect_receipt", record["body"]["stringValue"])
        attrs = {item["key"]: item["value"] for item in record["attributes"]}
        self.assertEqual("fx_1", attrs["effect_id"]["stringValue"])
        self.assertEqual("committed", attrs["outcome"]["stringValue"])

    def test_strict_attribute_allowlist_drops_unknown_keys(self) -> None:
        exporter = OtelExporter(_config())
        span = _stored_span(
            attributes={
                "provider": "deepseek",
                "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
                "payload": {"nested": "secret"},
            }
        )
        payload = exporter.build_payload(spans=[span])
        attrs = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        keys = {item["key"] for item in attrs}
        self.assertIn("provider", keys)
        self.assertNotIn("api_key", keys)
        self.assertNotIn("payload", keys)

    def test_gen_ai_semconv_attributes_are_exported(self) -> None:
        exporter = OtelExporter(_config())
        span = _stored_span(
            attributes={
                "provider": "deepseek",
                "model": "deepseek-chat",
                "gen_ai.system": "deepseek",
                "gen_ai.model.name": "deepseek-chat",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
            }
        )
        payload = exporter.build_payload(spans=[span])
        attrs = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        keys = {item["key"] for item in attrs}
        for key in (
            "gen_ai.system",
            "gen_ai.model.name",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
        ):
            self.assertIn(key, keys)

    def test_event_data_default_dropped_unknown_keys(self) -> None:
        exporter = OtelExporter(_config())
        event = _stored_event(
            data={
                "effect_id": "fx_1",
                "secret_token": "abc123",
                "environment": {"PATH": "/usr/bin"},
            }
        )
        payload = exporter.build_payload(events=[event])
        attrs = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
        keys = {item["key"] for item in attrs}
        self.assertIn("effect_id", keys)
        self.assertNotIn("secret_token", keys)
        self.assertNotIn("environment", keys)

    def test_ids_are_deterministic(self) -> None:
        exporter = OtelExporter(_config())
        first = exporter.build_payload(spans=[_stored_span()])
        second = exporter.build_payload(spans=[_stored_span()])
        a = first["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        b = second["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(a["traceId"], b["traceId"])
        self.assertEqual(a["spanId"], b["spanId"])

    def test_payload_pure_and_serializable(self) -> None:
        exporter = OtelExporter(_config())
        payload = exporter.build_payload(
            spans=[_stored_span()], events=[_stored_event()]
        )
        body = json.dumps(payload)
        self.assertIn("resourceSpans", body)
        self.assertIn("resourceLogs", body)

    def test_empty_payload_has_neither_section(self) -> None:
        exporter = OtelExporter(_config())
        payload = exporter.build_payload()
        self.assertNotIn("resourceSpans", payload)
        self.assertNotIn("resourceLogs", payload)


class ExportBehaviorTest(unittest.TestCase):
    def test_disabled_exporter_does_not_export(self) -> None:
        transport = _RecordingTransport()
        exporter = OtelExporter(
            OtelExporterConfig(mode=OtelExportMode.DISABLED),
            transport=transport,
        )
        self.assertFalse(exporter.enabled)
        result = exporter.export_batch(
            spans=[_stored_span()], events=[_stored_event()]
        )
        self.assertFalse(result)
        self.assertEqual([], transport.calls)

    def test_export_batch_sends_payload(self) -> None:
        transport = _RecordingTransport()
        exporter = OtelExporter(_config(), transport=transport)
        result = exporter.export_batch(spans=[_stored_span()], events=[])
        self.assertTrue(result)
        self.assertEqual(1, len(transport.calls))
        endpoint, body = transport.calls[0]
        self.assertEqual("http://127.0.0.1:4318/v1/traces", endpoint)
        self.assertIn(b"resourceSpans", body)

    def test_transport_failure_returns_false_without_raising(self) -> None:
        transport = _RecordingTransport()
        transport.fail = True
        exporter = OtelExporter(_config(), transport=transport)
        result = exporter.export_batch(spans=[_stored_span()])
        self.assertFalse(result)

    def test_empty_batch_returns_false(self) -> None:
        transport = _RecordingTransport()
        exporter = OtelExporter(_config(), transport=transport)
        self.assertFalse(exporter.export_batch())
        self.assertEqual([], transport.calls)

    def test_invalid_transport_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OtelExporter(_config(), transport="not-callable")


class SqliteIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _start_and_end_span(self) -> None:
        import asyncio

        handle = self.ledger.start_span(
            SpanSpec(
                trace=TraceContext(
                    trace_id="trace_1",
                    run_id="run_1",
                    correlation_id="corr_1",
                    span_id="span_1",
                ),
                kind=SpanKind.MODEL,
                name="provider.call",
                started_at="2026-09-05T00:00:00Z",
                monotonic_started=0.0,
                attributes={"provider": "deepseek", "model": "deepseek-chat"},
            )
        )
        asyncio.run(
            handle.end(
                SpanStatus.COMPLETED,
                ended_at="2026-09-05T00:00:01Z",
                monotonic_ended=1.0,
            )
        )

    def test_export_run_from_sqlite_ledger(self) -> None:
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
        self._start_and_end_span()
        transport = _RecordingTransport()
        exporter = OtelExporter(_config(), transport=transport)
        ok = exporter.export_run(self.ledger, "run_1")
        self.assertTrue(ok)
        body = transport.calls[0][1]
        payload = json.loads(body)
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        logs = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(1, len(spans))
        self.assertEqual(1, len(logs))
        # Unknown event data key "run" must not reach the payload.
        log_attrs = logs[0]["attributes"]
        self.assertNotIn("run", {item["key"] for item in log_attrs})

    def test_export_run_disabled_returns_false(self) -> None:
        transport = _RecordingTransport()
        exporter = OtelExporter(
            OtelExporterConfig(mode=OtelExportMode.DISABLED),
            transport=transport,
        )
        self.assertFalse(exporter.export_run(self.ledger, "run_1"))


if __name__ == "__main__":
    unittest.main()
