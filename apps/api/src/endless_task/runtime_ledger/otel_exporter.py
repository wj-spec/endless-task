"""Optional OTLP/HTTP trace exporter (M6 OE-5).

08 §12 OE-5 rules, made concrete:

- **OTel is an exporter, not the trace source of truth**: the SQLite
  ledger (OE-0) remains authoritative; this module only reads persisted
  spans/events and serializes them to an OTLP/HTTP JSON payload.
- **Default off**: no transport is started unless an explicit mode
  enables it (``ENDLESS_TASK_OTEL_EXPORT=otlp-http``); the parser is
  strict and rejects unknown values.
- **Export failure never breaks the main path**: ``export_batch``
  returns ``False`` on transport failure instead of raising into the
  caller; only misconfiguration raises at construction.
- **Strict attribute allowlist**: a span attribute is exported only when
  its key is in the exporter's allowlist (defaults to the protocol
  ``TRACE_ATTRIBUTE_ALLOWLIST``) and its value is a JSON-scalar. Anything
  else is dropped before serialization — secrets can never leak through
  the exporter by construction.

The payload builder is pure and dependency-free (OTLP/HTTP JSON uses the
documented JSON mapping of the OTLP protobufs); the transport seam takes
the encoded body so tests never need a collector.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .protocol import (
    TRACE_ATTRIBUTE_ALLOWLIST,
    SpanStatus,
)
from .sqlite_recorder import StoredLedgerEvent, StoredSpan

OTEL_SCHEMA_URL = "https://opentelemetry.io/schemas/1.24.0"
DEFAULT_SERVICE_NAME = "endless-task"
MAX_EXPORT_BODY_BYTES = 4 * 1024 * 1024  # conservative OTLP/HTTP cap


class OtelExportMode(str, Enum):
    DISABLED = "0"
    OTLP_HTTP = "otlp-http"

    @classmethod
    def is_enabled(cls, value: str) -> bool:
        return parse_otel_export_mode(value) is not cls.DISABLED


def parse_otel_export_mode(value: str) -> OtelExportMode:
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", "disabled", ""}:
        return OtelExportMode.DISABLED
    if normalized in {"1", "true", "yes", "on", "otlp", "otlp-http"}:
        return OtelExportMode.OTLP_HTTP
    raise ValueError("ENDLESS_TASK_OTEL_EXPORT 只允许 0|otlp-http")


#: span.kind -> OTLP SpanKind numeric enum value.
_OTLP_SPAN_KIND = {
    "internal": 1,  # SPAN_KIND_INTERNAL
    "server": 2,  # SPAN_KIND_SERVER
    "client": 3,  # SPAN_KIND_CLIENT
    "producer": 4,  # SPAN_KIND_PRODUCER
    "consumer": 5,  # SPAN_KIND_CONSUMER
}

#: RuntimeLedger SpanKind -> OTLP kind name (internal spans stay internal;
#: provider/model become client, effect stays internal).
_SPAN_KIND_NAME = {
    "internal": "internal",
    "context": "internal",
    "model": "client",
    "provider": "client",
    "tool": "client",
    "effect": "internal",
    "child": "client",
}

#: Ledger event data keys that are safe to export. Event ``data`` is the
#: only free-form record channel, so it defaults to **not** exported:
#: only protocol-known fields written by the recorders/effect receipts
#: (already allowlisted at the source) pass. Unknown keys are dropped.
EVENT_DATA_ALLOWLIST = frozenset(
    {
        # effect receipts (sqlite_recorder.record_effect)
        "effect_id",
        "tool_call_id",
        "effect_type",
        "outcome",
        "backend",
        "safe_summary",
        # span projection events (recorder._DeferredSpanHandle)
        "span_name",
        "kind",
        "status",
        "duration_ms",
        "diagnostic_code",
    }
)

_STATUS_CODE = {
    "completed": 1,  # STATUS_CODE_OK
    "failed": 2,  # STATUS_CODE_ERROR
    "cancelled": 0,  # STATUS_CODE_UNSET
    "running": 0,
}


@dataclass(frozen=True)
class OtelExporterConfig:
    """Exporter configuration. Everything not an endpoint is defaulted."""

    mode: OtelExportMode = OtelExportMode.DISABLED
    endpoint: str = "http://127.0.0.1:4318/v1/traces"
    service_name: str = DEFAULT_SERVICE_NAME
    attribute_allowlist: frozenset[str] = TRACE_ATTRIBUTE_ALLOWLIST

    def __post_init__(self) -> None:
        if not isinstance(self.mode, OtelExportMode):
            raise ValueError("mode must be an OtelExportMode")
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an http(s) URL")
        if not self.service_name.strip():
            raise ValueError("service_name must be non-empty")


class OtelExporter:
    """Reads persisted ledger records and exports them to OTLP/HTTP JSON.

    Construction validates configuration; ``export_batch`` never raises
    on transport failure — it returns ``False`` so an observability
    export problem can never break the agent run (08 §OE-5).
    """

    def __init__(
        self,
        config: OtelExporterConfig,
        *,
        transport: Optional[Callable[[str, bytes], int]] = None,
    ) -> None:
        self._config = config
        if transport is not None and not callable(transport):
            raise ValueError("transport must be callable")
        self._transport = transport or self._default_transport

    @property
    def config(self) -> OtelExporterConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.mode is not OtelExportMode.DISABLED

    def build_payload(
        self,
        *,
        spans: Sequence[StoredSpan] = (),
        events: Sequence[StoredLedgerEvent] = (),
    ) -> Mapping[str, Any]:
        """Serialize spans/events to the OTLP/HTTP JSON export body.

        Pure and deterministic; only allowlisted scalar attributes are
        emitted. Events export as log records (they are journal facts,
        not span-attached), spans export as trace spans.
        """
        resource_spans = (
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": self._config.service_name},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "endless_task.runtime_ledger"},
                        "spans": [_span_to_otlp(span, self._config) for span in spans],
                    }
                ],
                "schemaUrl": OTEL_SCHEMA_URL,
            }
            if spans
            else None
        )
        resource_logs = (
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": self._config.service_name},
                        }
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "endless_task.runtime_ledger"},
                        "logRecords": [
                            _event_to_log_record(event, self._config)
                            for event in events
                        ],
                    }
                ],
                "schemaUrl": OTEL_SCHEMA_URL,
            }
            if events
            else None
        )
        payload: dict[str, Any] = {}
        if resource_spans is not None:
            payload["resourceSpans"] = [resource_spans]
        if resource_logs is not None:
            payload["resourceLogs"] = [resource_logs]
        return payload

    def encode_payload(
        self,
        *,
        spans: Sequence[StoredSpan] = (),
        events: Sequence[StoredLedgerEvent] = (),
    ) -> bytes:
        body = json.dumps(
            self.build_payload(spans=spans, events=events),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_EXPORT_BODY_BYTES:
            raise ValueError(
                f"export body {len(body)} bytes exceeds {MAX_EXPORT_BODY_BYTES}"
            )
        return body

    def export_batch(
        self,
        *,
        spans: Sequence[StoredSpan] = (),
        events: Sequence[StoredLedgerEvent] = (),
    ) -> bool:
        """Export one batch; returns success. Never raises on transport."""
        if not self.enabled or (not spans and not events):
            return False
        try:
            body = self.encode_payload(spans=spans, events=events)
            self._transport(self._config.endpoint, body)
            return True
        except Exception:
            # OE-5: exporter failure must not affect the main path.
            return False

    def export_run(self, ledger, run_id: str) -> bool:
        """Export one run's persisted spans/events from a ledger sink."""
        return self.export_batch(
            spans=ledger.spans_for_run(run_id),
            events=ledger.events_for_run(run_id),
        )

    @staticmethod
    def _default_transport(endpoint: str, body: bytes) -> int:
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "endless-task-runtime-ledger/otel-exporter",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status


def _span_to_otlp(
    span: StoredSpan,
    config: OtelExporterConfig,
) -> Mapping[str, Any]:
    status_code = _STATUS_CODE.get(span.status, 0)
    result: dict[str, Any] = {
        "traceId": _id_to_otlp(span.trace_id, width=16),
        "spanId": _id_to_otlp(span.span_id, width=8),
        "name": span.name,
        "kind": _OTLP_SPAN_KIND.get(_SPAN_KIND_NAME.get(span.kind, "internal"), 1),
        "startTimeUnixNano": _iso_to_nanos(span.started_at),
        "status": {"code": status_code},
    }
    if span.parent_span_id:
        result["parentSpanId"] = _id_to_otlp(span.parent_span_id, width=8)
    if span.ended_at:
        result["endTimeUnixNano"] = _iso_to_nanos(span.ended_at)
    attributes = _export_attributes(span.attributes, config)
    if span.diagnostic_code:
        attributes.append(
            _attribute("error.code", str(span.diagnostic_code))
        )
    if attributes:
        result["attributes"] = attributes
    if status_code == 2 and span.diagnostic_code:
        result["status"]["message"] = span.diagnostic_code
    return result


def _event_to_log_record(
    event: StoredLedgerEvent,
    config: OtelExporterConfig,
) -> Mapping[str, Any]:
    record: dict[str, Any] = {
        "timeUnixNano": _iso_to_nanos(event.occurred_at),
        "severityNumber": 13 if event.safety_critical else 9,  # WARN / INFO
        "severityText": "WARN" if event.safety_critical else "INFO",
        "body": {"stringValue": event.event_type},
    }
    attributes: list[Mapping[str, Any]] = [
        _attribute("event.id", event.event_id),
        _attribute("event.safety_critical", str(event.safety_critical).lower()),
        _attribute("run.id", event.run_id),
        _attribute("correlation.id", event.correlation_id),
    ]
    attributes.extend(_export_attributes(event.data, config, allowlist=EVENT_DATA_ALLOWLIST))
    record["attributes"] = attributes
    return record


def _export_attributes(
    attributes: Mapping[str, Any],
    config: OtelExporterConfig,
    *,
    allowlist: Optional[frozenset[str]] = None,
) -> list[Mapping[str, Any]]:
    """Strict allowlist: drop unknown keys and non-scalar values."""
    effective = config.attribute_allowlist if allowlist is None else allowlist
    exported: list[Mapping[str, Any]] = []
    for key, value in attributes.items():
        if key not in effective:
            continue
        if isinstance(value, bool):
            exported.append(_attribute(key, value))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            exported.append(_attribute(key, value))
        elif isinstance(value, str):
            exported.append(_attribute(key, value))
        # lists/dicts/None are dropped: exporter never widens the schema.
    return exported


def _attribute(key: str, value: Any) -> Mapping[str, Any]:
    if isinstance(value, bool):
        encoded: Mapping[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _id_to_otlp(identifier: str, *, width: int) -> str:
    """Deterministic 16/8-byte id from an internal identifier.

    Internal trace/span ids are identifiers, not W3C 128-bit trace ids;
    the exporter maps them deterministically so re-export of the same
    ledger rows yields the same OTLP ids (documented in 08 §22.4).
    """
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    import base64

    return base64.b64encode(digest[:width]).decode("ascii")


_ISO_MS_RE = re.compile(
    r"^(?P<rest>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d{1,9})?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


def _iso_to_nanos(value: str) -> int:
    """Parse ledger ISO-8601 timestamps to Unix nanoseconds (int)."""
    match = _ISO_MS_RE.match(value)
    if match is None:
        raise ValueError(f"unsupported timestamp: {value!r}")
    normalized = match.group("rest")
    zone = match.group("zone")
    if zone == "Z":
        normalized += "+00:00"
    else:
        normalized += zone
    instant = datetime.fromisoformat(normalized)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return int(instant.timestamp() * 1_000_000_000)


__all__ = [
    "EVENT_DATA_ALLOWLIST",
    "OtelExporter",
    "OtelExporterConfig",
    "OtelExportMode",
    "parse_otel_export_mode",
]
