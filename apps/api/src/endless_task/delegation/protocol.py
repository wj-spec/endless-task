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
    require_text,
)

DELEGATION_PROTOCOL_VERSION = 1


class WorkspaceMode(str, Enum):
    NONE = "none"
    READ_ONLY_SHARED = "read_only_shared"
    ISOLATED_SNAPSHOT = "isolated_snapshot"
    ISOLATED_WORKTREE = "isolated_worktree"
    SHARED_WRITE_SERIALIZED = "shared_write_serialized"


class ChildStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ChildOutputSchema:
    name: str
    schema: Mapping[str, Any]
    max_characters: int = 20_000
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_output_schema")
        object.__setattr__(self, "name", require_identifier(self.name, field_name="name"))
        object.__setattr__(self, "schema", freeze_json_object(self.schema, field_name="schema"))
        if self.max_characters <= 0:
            raise AgentPlatformError("invalid_delegation_value", "Child output limit must be positive")


@dataclass(frozen=True)
class ChildModelPolicy:
    preferred_model: Optional[str] = None
    allow_fallback: bool = False
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_model_policy")
        if self.preferred_model is not None:
            object.__setattr__(
                self,
                "preferred_model",
                require_identifier(self.preferred_model, field_name="preferred_model"),
            )
        if not isinstance(self.allow_fallback, bool):
            raise AgentPlatformError("invalid_delegation_value", "Child model fallback flag must be boolean")


@dataclass(frozen=True)
class ChildContextPolicy:
    excerpts: tuple[str, ...] = ()
    file_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    checkpoint_id: Optional[str] = None
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_context_policy")
        for field_name in ("excerpts", "file_refs", "artifact_refs", "memory_ids"):
            object.__setattr__(
                self,
                field_name,
                tuple(
                    require_identifier(value, field_name=field_name, max_length=32_768)
                    for value in getattr(self, field_name)
                ),
            )
        if self.checkpoint_id is not None:
            object.__setattr__(
                self,
                "checkpoint_id",
                require_identifier(self.checkpoint_id, field_name="checkpoint_id"),
            )


@dataclass(frozen=True)
class SpawnSpec:
    task: str
    expected_output: ChildOutputSchema
    capability_profile: str
    requested_capabilities: frozenset[str]
    tool_allowlist: tuple[str, ...]
    model_policy: ChildModelPolicy
    context_policy: ChildContextPolicy
    workspace_mode: WorkspaceMode
    timeout_seconds: float
    parent_run_id: str
    parent_tool_call_id: str
    max_model_turns: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    #: M4B P2b: scratch workspace id prepared for an ISOLATED_SNAPSHOT child
    #: (filled by the handler before spawn when isolated write is requested).
    child_workspace_id: Optional[str] = None
    protocol_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "spawn_spec")
        object.__setattr__(self, "task", require_text(self.task, field_name="task", max_length=32_768))
        for field_name in ("capability_profile", "parent_run_id", "parent_tool_call_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "requested_capabilities",
            frozenset(
                require_identifier(value, field_name="requested_capability")
                for value in self.requested_capabilities
            ),
        )
        object.__setattr__(
            self,
            "tool_allowlist",
            tuple(require_identifier(value, field_name="tool_allowlist") for value in self.tool_allowlist),
        )
        if not isinstance(self.workspace_mode, WorkspaceMode):
            raise AgentPlatformError("invalid_delegation_value", "Workspace mode must use a protocol enum value")
        if not 0 < self.timeout_seconds <= 86_400:
            raise AgentPlatformError("invalid_delegation_value", "Child timeout must be within (0, 86400]")
        for field_name in ("max_model_turns", "max_input_tokens", "max_output_tokens"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise AgentPlatformError("invalid_delegation_value", f"{field_name} must be positive when provided")


@dataclass(frozen=True)
class Finding:
    title: str
    detail: str
    source_refs: tuple[str, ...] = ()
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_finding")
        object.__setattr__(self, "title", require_text(self.title, field_name="title", max_length=240))
        object.__setattr__(self, "detail", require_text(self.detail, field_name="detail", max_length=4_096))
        object.__setattr__(
            self,
            "source_refs",
            tuple(require_identifier(value, field_name="source_ref", max_length=4_096) for value in self.source_refs),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    safe_label: str
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_artifact_ref")
        for field_name in ("artifact_id", "kind"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "safe_label",
            require_text(self.safe_label, field_name="safe_label", max_length=240),
        )


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    model_turns: int = 0
    tool_calls: int = 0
    schema_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.schema_version, "child_usage_summary")
        for field_name in ("input_tokens", "output_tokens", "model_turns", "tool_calls"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise AgentPlatformError("invalid_delegation_value", f"{field_name} must be a non-negative integer")


@dataclass(frozen=True)
class ChildOutcome:
    child_run_id: str
    status: ChildStatus
    summary: str
    findings: tuple[Finding, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    effects: tuple[EffectReceiptRef, ...] = ()
    open_questions: tuple[str, ...] = ()
    confidence: Optional[float] = None
    usage: UsageSummary = field(default_factory=UsageSummary)
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = DELEGATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "child_outcome")
        object.__setattr__(
            self,
            "child_run_id",
            require_identifier(self.child_run_id, field_name="child_run_id"),
        )
        if not isinstance(self.status, ChildStatus):
            raise AgentPlatformError("invalid_delegation_value", "Child status must use a protocol enum value")
        object.__setattr__(
            self,
            "summary",
            require_text(self.summary, field_name="summary", max_length=20_000),
        )
        object.__setattr__(
            self,
            "open_questions",
            tuple(require_text(value, field_name="open_question", max_length=2_048) for value in self.open_questions),
        )
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise AgentPlatformError("invalid_delegation_value", "Child outcome confidence must be within [0, 1]")


class DelegationRuntime(Protocol):
    async def spawn(self, spec: SpawnSpec) -> str: ...

    async def query(self, child_run_id: str) -> ChildOutcome: ...

    async def cancel(self, child_run_id: str, reason: str) -> None: ...


def intersect_capabilities(
    parent: frozenset[str],
    requested: frozenset[str],
    profile: frozenset[str],
    backend: frozenset[str],
    deployment: frozenset[str],
) -> frozenset[str]:
    return parent & requested & profile & backend & deployment


def _validate_version(version: int, protocol: str) -> None:
    require_protocol_version(version, expected=DELEGATION_PROTOCOL_VERSION, protocol=protocol)