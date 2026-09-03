from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from endless_task.agent_platform import (
    AgentPlatformError,
    CancellationSignal,
    EffectReceiptRef,
    JsonValue,
    SafeDiagnostic,
    freeze_json,
    freeze_json_object,
    plain_json,
    require_identifier,
    require_protocol_version,
    require_text,
)

TOOL_PROTOCOL_VERSION = 2


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    PROCESS = "process"
    NETWORK = "network"
    EXTERNAL_ACTION = "external_action"


class ApprovalPolicy(str, Enum):
    AUTO = "auto"
    ASK = "ask"
    REQUIRED = "required"
    FORBIDDEN_UNATTENDED = "forbidden_unattended"


class ToolExecutionMode(str, Enum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    EXCLUSIVE = "exclusive"
    PATH_SCOPED = "path_scoped"


class IdempotencyPolicy(str, Enum):
    SAFE = "safe"
    KEY_REQUIRED = "key_required"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class ToolOutcomeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    retryable_error_codes: frozenset[str] = frozenset()
    require_idempotency_key: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_protocol_version(self.schema_version, expected=1, protocol="tool_retry_policy")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise AgentPlatformError(
                "invalid_retry_policy",
                "Tool retry max_attempts must be between 1 and 10",
            )
        object.__setattr__(
            self,
            "retryable_error_codes",
            frozenset(
                require_identifier(code, field_name="retryable_error_code")
                for code in self.retryable_error_codes
            ),
        )
        if not isinstance(self.require_idempotency_key, bool):
            raise AgentPlatformError(
                "invalid_retry_policy",
                "require_idempotency_key must be boolean",
            )


@dataclass(frozen=True)
class ToolPresentation:
    category: str
    running_label: str
    completed_label: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_protocol_version(self.schema_version, expected=1, protocol="tool_presentation")
        object.__setattr__(
            self,
            "category",
            require_identifier(self.category, field_name="category"),
        )
        for field_name in ("running_label", "completed_label"):
            object.__setattr__(
                self,
                field_name,
                require_text(getattr(self, field_name), field_name=field_name, max_length=160),
            )


@dataclass(frozen=True)
class ToolDefinitionV2:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Optional[Mapping[str, Any]] = None
    effect: ToolEffect = ToolEffect.READ_ONLY
    approval: ApprovalPolicy = ApprovalPolicy.AUTO
    execution_mode: ToolExecutionMode = ToolExecutionMode.PARALLEL
    idempotency: IdempotencyPolicy = IdempotencyPolicy.UNKNOWN
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    required_capabilities: frozenset[str] = frozenset()
    timeout_seconds: float = 30.0
    max_output_characters: int = 200_000
    max_context_share: float = 0.3
    presentation: Optional[ToolPresentation] = None
    protocol_version: int = TOOL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=TOOL_PROTOCOL_VERSION,
            protocol="tool_definition",
        )
        object.__setattr__(self, "name", require_identifier(self.name, field_name="name", max_length=64))
        object.__setattr__(
            self,
            "description",
            require_text(self.description, field_name="description", max_length=1_024),
        )
        input_schema = _validated_schema(self.input_schema, field_name="input_schema")
        if input_schema.get("type") != "object":
            raise AgentPlatformError(
                "invalid_tool_schema",
                "Tool input schema root type must be object",
            )
        object.__setattr__(self, "input_schema", input_schema)
        if self.output_schema is not None:
            object.__setattr__(
                self,
                "output_schema",
                _validated_schema(self.output_schema, field_name="output_schema"),
            )
        for field_name, enum_type in (
            ("effect", ToolEffect),
            ("approval", ApprovalPolicy),
            ("execution_mode", ToolExecutionMode),
            ("idempotency", IdempotencyPolicy),
        ):
            if not isinstance(getattr(self, field_name), enum_type):
                raise AgentPlatformError(
                    "invalid_tool_definition",
                    f"{field_name} must use a protocol enum value",
                )
        if not 0 < self.timeout_seconds <= 600:
            raise AgentPlatformError(
                "invalid_tool_timeout",
                "Tool timeout must be greater than 0 and at most 600 seconds",
            )
        if self.max_output_characters <= 0:
            raise AgentPlatformError(
                "invalid_tool_output_limit",
                "Tool output limit must be positive",
            )
        if not 0 < self.max_context_share <= 1:
            raise AgentPlatformError(
                "invalid_context_share",
                "Tool max_context_share must be within (0, 1]",
            )
        object.__setattr__(
            self,
            "required_capabilities",
            frozenset(
                require_identifier(capability, field_name="required_capability")
                for capability in self.required_capabilities
            ),
        )


@dataclass(frozen=True)
class ToolExecutionRequest:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    conversation_id: str
    run_id: str
    model_turn_id: str
    correlation_id: str
    cancellation: CancellationSignal
    created_at: str
    legacy_turn_id: Optional[str] = None
    legacy_response_variant_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    protocol_version: int = TOOL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=TOOL_PROTOCOL_VERSION,
            protocol="tool_execution_request",
        )
        for field_name in (
            "call_id",
            "tool_name",
            "conversation_id",
            "run_id",
            "model_turn_id",
            "correlation_id",
            "created_at",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "arguments",
            freeze_json_object(self.arguments, field_name="arguments"),
        )
        for field_name in ("legacy_turn_id", "legacy_response_variant_id", "idempotency_key"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name=field_name),
                )
        if not callable(getattr(self.cancellation, "raise_if_cancelled", None)):
            raise AgentPlatformError(
                "invalid_cancellation_signal",
                "Tool execution requires a cancellation signal",
            )


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    status: ToolOutcomeStatus
    content: str
    structured_content: JsonValue = None
    diagnostic: Optional[SafeDiagnostic] = None
    effects: tuple[EffectReceiptRef, ...] = ()
    is_truncated: bool = False
    terminate: bool = False
    protocol_version: int = TOOL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=TOOL_PROTOCOL_VERSION,
            protocol="tool_outcome",
        )
        object.__setattr__(self, "call_id", require_identifier(self.call_id, field_name="call_id"))
        if not isinstance(self.status, ToolOutcomeStatus):
            raise AgentPlatformError(
                "invalid_tool_outcome",
                "Tool outcome status must use a protocol enum value",
            )
        if not isinstance(self.content, str):
            raise AgentPlatformError("invalid_tool_outcome", "Tool outcome content must be text")
        object.__setattr__(
            self,
            "structured_content",
            freeze_json(self.structured_content, field_name="structured_content"),
        )
        if self.status is ToolOutcomeStatus.COMPLETED and self.diagnostic is not None:
            raise AgentPlatformError(
                "invalid_tool_outcome",
                "Completed tool outcomes cannot carry a failure diagnostic",
            )
        if self.status is not ToolOutcomeStatus.COMPLETED and self.diagnostic is None:
            raise AgentPlatformError(
                "invalid_tool_outcome",
                "Non-completed tool outcomes require a diagnostic",
            )
        if self.terminate and self.status is not ToolOutcomeStatus.COMPLETED:
            raise AgentPlatformError(
                "invalid_tool_outcome",
                "Only completed tool outcomes may terminate the agent loop",
            )
        if not isinstance(self.is_truncated, bool) or not isinstance(self.terminate, bool):
            raise AgentPlatformError(
                "invalid_tool_outcome",
                "Tool outcome flags must be boolean",
            )


class AgentToolV2(Protocol):
    definition: ToolDefinitionV2

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome: ...


def _validated_schema(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, Any]:
    schema = freeze_json_object(value, field_name=field_name)
    plain_schema = plain_json(schema)
    try:
        validator_for(plain_schema).check_schema(plain_schema)
    except SchemaError as error:
        raise AgentPlatformError(
            "invalid_tool_schema",
            f"{field_name} must be valid JSON Schema",
        ) from error
    return MappingProxyType(dict(schema))