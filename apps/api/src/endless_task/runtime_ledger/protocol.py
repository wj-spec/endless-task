from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    EffectReceipt,
    SafeDiagnostic,
    freeze_json_object,
    require_identifier,
    require_protocol_version,
)

TRACE_SCHEMA_VERSION = 1
TRACE_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "attempt",
        "backend",
        "cache_tokens",
        "catalog_generation",
        "child_count",
        "context_fingerprint",
        "effect_type",
        "finish_reason",
        "input_tokens",
        "model",
        "output_tokens",
        "profile",
        "provider",
        "retry_count",
        "status",
        "tool_name",
        # GenAI semantic conventions (D2): standard span attributes so the
        # OTLP exporter emits spec-conformant gen_ai.* on model spans.
        "gen_ai.system",
        "gen_ai.model.name",
        "gen_ai.operation.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.response.finish_reason",
    }
)


class SpanKind(str, Enum):
    INTERNAL = "internal"
    CONTEXT = "context"
    MODEL = "model"
    PROVIDER = "provider"
    TOOL = "tool"
    EFFECT = "effect"
    CHILD = "child"


class SpanStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    run_id: str
    correlation_id: str
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    model_turn_id: Optional[str] = None
    tool_execution_id: Optional[str] = None
    child_run_id: Optional[str] = None
    schema_version: int = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TRACE_SCHEMA_VERSION,
            protocol="trace_context",
        )
        for field_name in ("trace_id", "run_id", "correlation_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        for field_name in (
            "span_id",
            "parent_span_id",
            "model_turn_id",
            "tool_execution_id",
            "child_run_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name=field_name),
                )
        if self.parent_span_id is not None and self.span_id is None:
            raise AgentPlatformError("invalid_trace_value", "parent_span_id requires span_id")


@dataclass(frozen=True)
class SpanSpec:
    trace: TraceContext
    kind: SpanKind
    name: str
    started_at: str
    monotonic_started: float
    attributes: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TRACE_SCHEMA_VERSION,
            protocol="span_spec",
        )
        if not isinstance(self.kind, SpanKind):
            raise AgentPlatformError("invalid_trace_value", "Span kind must use a protocol enum value")
        object.__setattr__(self, "name", require_identifier(self.name, field_name="name"))
        object.__setattr__(
            self,
            "started_at",
            require_identifier(self.started_at, field_name="started_at"),
        )
        unknown = set(self.attributes) - TRACE_ATTRIBUTE_ALLOWLIST
        if unknown:
            raise AgentPlatformError("invalid_trace_value", f"Trace attributes are not allowlisted: {sorted(unknown)}")
        object.__setattr__(
            self,
            "attributes",
            freeze_json_object(self.attributes, field_name="attributes"),
        )


@dataclass(frozen=True)
class RuntimeLedgerEvent:
    event_id: str
    event_type: str
    occurred_at: str
    trace: TraceContext
    data: Mapping[str, Any] = field(default_factory=dict)
    safety_critical: bool = False
    schema_version: int = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TRACE_SCHEMA_VERSION,
            protocol="runtime_ledger_event",
        )
        for field_name in ("event_id", "event_type", "occurred_at"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        if not isinstance(self.safety_critical, bool):
            raise AgentPlatformError("invalid_trace_value", "safety_critical must be boolean")
        object.__setattr__(self, "data", freeze_json_object(self.data, field_name="data"))


@dataclass(frozen=True)
class CanonicalUsage:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    request_count: int
    occurred_at: str
    trace: TraceContext
    schema_version: int = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TRACE_SCHEMA_VERSION,
            protocol="canonical_usage",
        )
        for field_name in ("provider", "model", "occurred_at"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "request_count",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise AgentPlatformError("invalid_trace_value", f"{field_name} must be a non-negative integer")


class SpanHandle(Protocol):
    @property
    def context(self) -> TraceContext: ...

    async def end(
        self,
        status: SpanStatus,
        *,
        ended_at: str,
        monotonic_ended: float,
        diagnostic: Optional[SafeDiagnostic] = None,
    ) -> None: ...


class RuntimeLedger(Protocol):
    async def append_event(self, event: RuntimeLedgerEvent) -> None: ...

    def start_span(self, spec: SpanSpec) -> SpanHandle: ...

    async def record_effect(self, receipt: EffectReceipt, trace: TraceContext) -> None: ...

    async def record_usage(self, usage: CanonicalUsage) -> None: ...