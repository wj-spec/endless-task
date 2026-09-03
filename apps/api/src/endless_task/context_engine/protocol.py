from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    EffectReceiptRef,
    SafeDiagnostic,
    freeze_json_object,
    require_identifier,
    require_protocol_version,
)
from endless_task.runtime.provider import ProviderMessage
from endless_task.runtime_ledger import TraceContext

CONTEXT_PROTOCOL_VERSION = 1


class ContextInputKind(str, Enum):
    TRANSCRIPT = "transcript"
    TOOL_OUTCOME = "tool_outcome"
    STEERING = "steering"
    MEMORY = "memory"
    EFFECT = "effect"


class ContextSegmentKind(str, Enum):
    SYSTEM = "system"
    CURRENT_USER = "current_user"
    RECENT_HISTORY = "recent_history"
    CHECKPOINT = "checkpoint"
    MEMORY = "memory"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    OPTIONAL_INSTRUCTION = "optional_instruction"


class ContextTransform(str, Enum):
    INCLUDED = "included"
    OMITTED = "omitted"
    PRUNED = "pruned"
    SPILLED = "spilled"
    COMPACTED = "compacted"


@dataclass(frozen=True)
class ContextBudget:
    window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int = 0
    schema_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CONTEXT_PROTOCOL_VERSION,
            protocol="context_budget",
        )
        for field_name in ("window_tokens", "reserved_output_tokens", "safety_margin_tokens"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise AgentPlatformError("invalid_context_value", f"{field_name} must be a non-negative integer")
        if self.input_budget <= 0:
            raise AgentPlatformError("invalid_context_value", "Context input budget must be positive")

    @property
    def input_budget(self) -> int:
        return self.window_tokens - self.reserved_output_tokens - self.safety_margin_tokens


@dataclass(frozen=True)
class ContextSegment:
    kind: ContextSegmentKind
    source_ids: tuple[str, ...]
    trust_level: str
    priority: int
    estimated_tokens: int
    transform: ContextTransform
    transform_reason: Optional[str] = None
    schema_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CONTEXT_PROTOCOL_VERSION,
            protocol="context_segment",
        )
        if not isinstance(self.kind, ContextSegmentKind) or not isinstance(
            self.transform, ContextTransform
        ):
            raise AgentPlatformError("invalid_context_value", "Context segment enums must use protocol values")
        object.__setattr__(
            self,
            "source_ids",
            tuple(require_identifier(value, field_name="source_id") for value in self.source_ids),
        )
        object.__setattr__(
            self,
            "trust_level",
            require_identifier(self.trust_level, field_name="trust_level"),
        )
        if not isinstance(self.priority, int) or not isinstance(self.estimated_tokens, int):
            raise AgentPlatformError("invalid_context_value", "Context segment priority and token estimate must be integers")
        if self.estimated_tokens < 0:
            raise AgentPlatformError("invalid_context_value", "Context segment token estimate cannot be negative")
        if self.transform is not ContextTransform.INCLUDED and not self.transform_reason:
            raise AgentPlatformError("invalid_context_value", "Transformed context segments require a reason")


@dataclass(frozen=True)
class BootstrapRequest:
    run_id: str
    lane_id: str
    trace: TraceContext
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_bootstrap")
        _validate_run_and_trace(self.run_id, self.trace)
        object.__setattr__(self, "lane_id", require_identifier(self.lane_id, field_name="lane_id"))


@dataclass(frozen=True)
class ContextInput:
    run_id: str
    kind: ContextInputKind
    source_id: str
    payload: Mapping[str, Any]
    trace: TraceContext
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_input")
        _validate_run_and_trace(self.run_id, self.trace)
        if not isinstance(self.kind, ContextInputKind):
            raise AgentPlatformError("invalid_context_value", "Context input kind must use a protocol enum value")
        object.__setattr__(self, "source_id", require_identifier(self.source_id, field_name="source_id"))
        object.__setattr__(self, "payload", freeze_json_object(self.payload, field_name="payload"))


@dataclass(frozen=True)
class ContextRequest:
    run_id: str
    model_turn_id: str
    lane_id: str
    budget: ContextBudget
    trace: TraceContext
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_request")
        _validate_run_and_trace(self.run_id, self.trace)
        for field_name in ("model_turn_id", "lane_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(self, "metadata", freeze_json_object(self.metadata, field_name="metadata"))


@dataclass(frozen=True)
class ContextSnapshot:
    messages: tuple[ProviderMessage, ...]
    segments: tuple[ContextSegment, ...]
    estimated_tokens: int
    reserved_output_tokens: int
    budget: ContextBudget
    fingerprint: str
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_snapshot")
        if self.estimated_tokens < 0 or self.reserved_output_tokens < 0:
            raise AgentPlatformError("invalid_context_value", "Context snapshot token counts cannot be negative")
        if self.reserved_output_tokens != self.budget.reserved_output_tokens:
            raise AgentPlatformError("invalid_context_value", "Context snapshot must preserve the requested output reservation")
        object.__setattr__(
            self,
            "fingerprint",
            require_identifier(self.fingerprint, field_name="fingerprint"),
        )


@dataclass(frozen=True)
class MaintenanceRequest:
    run_id: str
    trace: TraceContext
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_maintenance_request")
        _validate_run_and_trace(self.run_id, self.trace)


@dataclass(frozen=True)
class MaintenanceResult:
    changed: bool
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_maintenance_result")
        if not isinstance(self.changed, bool):
            raise AgentPlatformError("invalid_context_value", "Maintenance changed flag must be boolean")


@dataclass(frozen=True)
class CompactionRequest:
    run_id: str
    target_tokens: int
    trace: TraceContext
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_compaction_request")
        _validate_run_and_trace(self.run_id, self.trace)
        if not isinstance(self.target_tokens, int) or self.target_tokens <= 0:
            raise AgentPlatformError("invalid_context_value", "Compaction target_tokens must be positive")


@dataclass(frozen=True)
class CompactionResult:
    checkpoint_id: Optional[str]
    released_tokens: int
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_compaction_result")
        if self.checkpoint_id is not None:
            object.__setattr__(
                self,
                "checkpoint_id",
                require_identifier(self.checkpoint_id, field_name="checkpoint_id"),
            )
        if not isinstance(self.released_tokens, int) or self.released_tokens < 0:
            raise AgentPlatformError("invalid_context_value", "Compaction released_tokens cannot be negative")


@dataclass(frozen=True)
class TurnOutcome:
    run_id: str
    model_turn_id: str
    context_fingerprint: str
    effects: tuple[EffectReceiptRef, ...]
    trace: TraceContext
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_turn_outcome")
        _validate_run_and_trace(self.run_id, self.trace)
        for field_name in ("model_turn_id", "context_fingerprint"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = CONTEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_request_version(self.protocol_version, "context_commit_result")
        if not isinstance(self.committed, bool):
            raise AgentPlatformError("invalid_context_value", "Commit result flag must be boolean")


class ContextEngine(Protocol):
    async def bootstrap(self, request: BootstrapRequest) -> None: ...

    async def ingest(self, event: ContextInput) -> None: ...

    async def assemble(self, request: ContextRequest) -> ContextSnapshot: ...

    async def maintain(self, request: MaintenanceRequest) -> MaintenanceResult: ...

    async def compact(self, request: CompactionRequest) -> CompactionResult: ...

    async def commit_turn(self, outcome: TurnOutcome) -> CommitResult: ...


def _validate_request_version(version: int, protocol: str) -> None:
    require_protocol_version(version, expected=CONTEXT_PROTOCOL_VERSION, protocol=protocol)


def _validate_run_and_trace(run_id: str, trace: TraceContext) -> None:
    normalized = require_identifier(run_id, field_name="run_id")
    if normalized != trace.run_id:
        raise AgentPlatformError("invalid_context_value", "Context request and trace must reference the same run")