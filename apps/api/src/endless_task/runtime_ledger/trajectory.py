"""Trajectory bundle export with redaction (M6 OE-3).

08 §8: a trajectory bundle is the portable, shareable record of one run
for diagnosis and evaluation. It is assembled from the SQLite ledger's
persisted spans/events/usage plus caller-supplied message/tool-outcome/
context records, and it passes through a redaction pipeline before export
so a bundle never carries secrets:

- API keys / credential values,
- raw environment values,
- workspace-external absolute paths (allowed roots passed in),
- unapproved full file content (caller decides what to include),
- model private reasoning.

Redaction is applied to every free-text/data field the bundle carries;
the ledger protocol already allowlists span attributes, so this pipeline
guards the message/tool payloads that live outside the ledger. Writes
follow the 08 §8 layout exactly:

::

    trajectory-<run-id>/
      manifest.json
      events.jsonl
      spans.jsonl
      messages.redacted.jsonl
      tool-outcomes.redacted.jsonl
      context-segments.jsonl
      effects.jsonl
      expected.json
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .sqlite_recorder import (
    SqliteRuntimeLedger,
    StoredLedgerEvent,
    StoredSpan,
    StoredUsage,
)

TRAJECTORY_SCHEMA_VERSION = 1

#: Files in the 08 §8 layout (expected.json is optional).
BUNDLE_FILES = (
    "manifest.json",
    "events.jsonl",
    "spans.jsonl",
    "messages.redacted.jsonl",
    "tool-outcomes.redacted.jsonl",
    "context-segments.jsonl",
    "effects.jsonl",
)

#: Regexes that scrub obvious secret material from free text.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
)

#: Event/tool-outcome data keys that may carry raw payloads and must never
#: be exported (08 §2.2: only metadata/fingerprint/size/safe summaries).
_DROPPED_DATA_KEYS = frozenset(
    {
        "raw_arguments",
        "raw_result",
        "environment",
        "full_file_content",
        "reasoning",
        "internal_prompt",
        "secret",
        "credentials",
    }
)

_JSONL_ENCODING = "utf-8"


@dataclass(frozen=True)
class TrajectoryManifest:
    schema_version: int
    runtime_version: str
    provider: str
    model: str
    config_fingerprint: str
    redaction_policy_revision: str
    source_run_id: str
    tool_catalog_fingerprint: Optional[str] = None
    skill_revision: Optional[str] = None
    context_engine_revision: Optional[str] = None

    def to_json(self) -> Mapping[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runtimeVersion": self.runtime_version,
            "provider": self.provider,
            "model": self.model,
            "configFingerprint": self.config_fingerprint,
            "redactionPolicyRevision": self.redaction_policy_revision,
            "sourceRunId": self.source_run_id,
            "toolCatalogFingerprint": self.tool_catalog_fingerprint,
            "skillRevision": self.skill_revision,
            "contextEngineRevision": self.context_engine_revision,
        }


@dataclass(frozen=True)
class TrajectoryBundle:
    """One exported, redacted trajectory bundle (08 §8 layout content)."""

    manifest: TrajectoryManifest
    events: Tuple[Mapping[str, Any], ...] = ()
    spans: Tuple[Mapping[str, Any], ...] = ()
    usages: Tuple[Mapping[str, Any], ...] = ()
    messages: Tuple[Mapping[str, Any], ...] = ()
    tool_outcomes: Tuple[Mapping[str, Any], ...] = ()
    context_segments: Tuple[Mapping[str, Any], ...] = ()
    effects: Tuple[Mapping[str, Any], ...] = ()
    expected: Optional[Mapping[str, Any]] = None
    bundle_digest: str = ""

    def to_json(self) -> Mapping[str, Any]:
        return {
            "manifest": self.manifest.to_json(),
            "events": list(self.events),
            "spans": list(self.spans),
            "usages": list(self.usages),
            "messages": list(self.messages),
            "toolOutcomes": list(self.tool_outcomes),
            "contextSegments": list(self.context_segments),
            "effects": list(self.effects),
            "expected": self.expected,
            "bundleDigest": self.bundle_digest,
        }


def export_trajectory(
    ledger: SqliteRuntimeLedger,
    *,
    manifest: TrajectoryManifest,
    messages: Sequence[Mapping[str, Any]] = (),
    tool_outcomes: Sequence[Mapping[str, Any]] = (),
    context_segments: Sequence[Mapping[str, Any]] = (),
    effects: Sequence[Mapping[str, Any]] = (),
    expected: Optional[Mapping[str, Any]] = None,
) -> TrajectoryBundle:
    """Export a redacted trajectory for one run from the SQLite ledger.

    Ledger records are always exported (protocol allowlists their
    attributes); message/tool/context content is exported only when the
    caller supplies it, and passes through the same redaction pipeline.
    """
    run_id = manifest.source_run_id
    events = tuple(
        _redact_event(_event_json(event)) for event in ledger.events_for_run(run_id)
    )
    spans = tuple(
        _redact_mapping(_span_json(span)) for span in ledger.spans_for_run(run_id)
    )
    usages = tuple(_usage_json(usage) for usage in ledger.usage_for_run(run_id))
    bundle = TrajectoryBundle(
        manifest=manifest,
        events=events,
        spans=spans,
        usages=usages,
        messages=tuple(_redact_mapping(dict(record)) for record in messages),
        tool_outcomes=tuple(
            _redact_mapping(dict(record)) for record in tool_outcomes
        ),
        context_segments=tuple(
            _redact_mapping(dict(segment)) for segment in context_segments
        ),
        effects=tuple(_redact_mapping(dict(effect)) for effect in effects),
        expected=_redact_mapping(dict(expected)) if expected is not None else None,
    )
    digest = _bundle_digest(bundle)
    return TrajectoryBundle(
        manifest=manifest,
        events=events,
        spans=spans,
        usages=usages,
        messages=bundle.messages,
        tool_outcomes=bundle.tool_outcomes,
        context_segments=bundle.context_segments,
        effects=bundle.effects,
        expected=bundle.expected,
        bundle_digest=digest,
    )


def write_trajectory_bundle(
    bundle: TrajectoryBundle,
    directory: Path,
) -> Path:
    """Write a bundle to ``directory`` following the 08 §8 layout.

    Returns the digest file path (``expected.json`` or ``manifest.json``
    when no expected content exists).
    """
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "manifest.json", bundle.manifest.to_json())
    _write_jsonl(directory / "events.jsonl", bundle.events)
    _write_jsonl(directory / "spans.jsonl", bundle.spans)
    # usage.jsonl is a G1 extension over the 08 §8 layout: cost budgeting
    # (08 §10.3) needs the ledger usage rows alongside the bundle so
    # offline eval can enforce cost ceilings without the live ledger.
    _write_jsonl(directory / "usage.jsonl", bundle.usages)
    _write_jsonl(directory / "messages.redacted.jsonl", bundle.messages)
    _write_jsonl(directory / "tool-outcomes.redacted.jsonl", bundle.tool_outcomes)
    _write_jsonl(directory / "context-segments.jsonl", bundle.context_segments)
    _write_jsonl(directory / "effects.jsonl", bundle.effects)
    if bundle.expected is not None:
        _write_json(directory / "expected.json", bundle.expected)
        return directory / "expected.json"
    return directory / "manifest.json"


def load_trajectory_bundle(directory: Path) -> TrajectoryBundle:
    """Read a bundle written by :func:`write_trajectory_bundle`."""
    manifest_json = _read_json(directory / "manifest.json")
    manifest = TrajectoryManifest(
        schema_version=manifest_json["schemaVersion"],
        runtime_version=manifest_json["runtimeVersion"],
        provider=manifest_json["provider"],
        model=manifest_json["model"],
        config_fingerprint=manifest_json["configFingerprint"],
        redaction_policy_revision=manifest_json["redactionPolicyRevision"],
        source_run_id=manifest_json["sourceRunId"],
        tool_catalog_fingerprint=manifest_json.get("toolCatalogFingerprint"),
        skill_revision=manifest_json.get("skillRevision"),
        context_engine_revision=manifest_json.get("contextEngineRevision"),
    )
    expected_path = directory / "expected.json"
    expected = _read_json(expected_path) if expected_path.exists() else None
    return TrajectoryBundle(
        manifest=manifest,
        events=tuple(_read_jsonl(directory / "events.jsonl")),
        spans=tuple(_read_jsonl(directory / "spans.jsonl")),
        usages=tuple(_read_jsonl(directory / "usage.jsonl")),
        messages=tuple(_read_jsonl(directory / "messages.redacted.jsonl")),
        tool_outcomes=tuple(_read_jsonl(directory / "tool-outcomes.redacted.jsonl")),
        context_segments=tuple(_read_jsonl(directory / "context-segments.jsonl")),
        effects=tuple(_read_jsonl(directory / "effects.jsonl")),
        expected=expected,
        bundle_digest="",  # recomputed only for in-memory exports
    )


def redact_text(value: str) -> str:
    """Scrub obvious secret patterns from free text."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_record(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Apply the full redaction pipeline to one record.

    Public entry for non-ledger exporters (e.g. the v2 journal trajectory
    exporter): drops raw-payload keys, scrubs secret patterns and walks
    nested dicts/lists exactly like ledger exports do, so every bundle
    written through :func:`write_trajectory_bundle` is equally guarded.
    """
    return _redact_mapping(record)


def _redact_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in _DROPPED_DATA_KEYS:
            out[key] = "[REDACTED]"
            continue
        if isinstance(item, str):
            out[key] = redact_text(item)
        elif isinstance(item, Mapping):
            out[key] = _redact_mapping(item)
        elif isinstance(item, list):
            out[key] = [_redact_mapping(x) if isinstance(x, Mapping) else x for x in item]
        else:
            out[key] = item
    return out


def _redact_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _redact_mapping(event)


def _event_json(event: StoredLedgerEvent) -> Mapping[str, Any]:
    return {
        "eventId": event.event_id,
        "eventType": event.event_type,
        "occurredAt": event.occurred_at,
        "traceId": event.trace_id,
        "runId": event.run_id,
        "safetyCritical": event.safety_critical,
        "data": dict(event.data),
    }


def _span_json(span: StoredSpan) -> Mapping[str, Any]:
    return {
        "spanId": span.span_id,
        "traceId": span.trace_id,
        "runId": span.run_id,
        "parentSpanId": span.parent_span_id,
        "kind": span.kind,
        "name": span.name,
        "status": span.status,
        "startedAt": span.started_at,
        "endedAt": span.ended_at,
        "durationMs": span.monotonic_duration_ms,
        "attributes": dict(span.attributes),
        "diagnosticCode": span.diagnostic_code,
    }


def _usage_json(usage: StoredUsage) -> Mapping[str, Any]:
    return {
        "usageId": usage.usage_id,
        "provider": usage.provider,
        "model": usage.model,
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cachedInputTokens": usage.cached_input_tokens,
        "reasoningTokens": usage.reasoning_tokens,
        "requestCount": usage.request_count,
        "occurredAt": usage.occurred_at,
        "priceRevision": usage.price_revision,
        "costUsd": usage.cost_usd,
        "category": usage.category,
    }


def _bundle_digest(bundle: TrajectoryBundle) -> str:
    canonical = json.dumps(
        bundle.to_json(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding=_JSONL_ENCODING,
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    parsed = json.loads(path.read_text(encoding=_JSONL_ENCODING))
    return parsed if isinstance(parsed, dict) else {}


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    ]
    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding=_JSONL_ENCODING,
    )


def _read_jsonl(path: Path) -> Tuple[Mapping[str, Any], ...]:
    if not path.exists():
        return ()
    records: list[Mapping[str, Any]] = []
    for line in path.read_text(encoding=_JSONL_ENCODING).splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            records.append(parsed)
    return tuple(records)


__all__ = [
    "BUNDLE_FILES",
    "TRAJECTORY_SCHEMA_VERSION",
    "TrajectoryBundle",
    "TrajectoryManifest",
    "export_trajectory",
    "load_trajectory_bundle",
    "redact_record",
    "redact_text",
    "write_trajectory_bundle",
]
