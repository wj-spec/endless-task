from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from .schema import ToolSchemaError, check_tool_schema


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_ACTION = "external_action"


class ToolApprovalMode(str, Enum):
    AUTO = "auto"
    REQUIRED = "required"


class ToolCallStatus(str, Enum):
    CREATED = "created"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ProgressReporter(Protocol):
    """工具执行进度的回调签名(可选能力,工具不实现则无中间进度)。"""

    def __call__(self, *, message: str, percent: Optional[float] = None) -> None:
        ...


class ToolValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolError(Exception):
    """A normalized tool failure safe to pass back through the Agent Runtime."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool,
        correlation_id: Optional[str] = None,
    ) -> None:
        if not isinstance(code, str) or not isinstance(safe_message, str):
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error code and safe message must be text",
            )
        if not code.strip() or not safe_message.strip():
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error code and safe message cannot be empty",
            )
        if not isinstance(retryable, bool):
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error retryable flag must be boolean",
            )
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.correlation_id = correlation_id


def _validate_json_keys(value: Any, *, field_name: str) -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ToolValidationError(
                    "invalid_json_object",
                    f"{field_name} object keys must be text",
                )
            _validate_json_keys(nested_value, field_name=field_name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_keys(item, field_name=field_name)


def _validated_json_object(
    value: Mapping[str, Any],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolValidationError("invalid_json_object", f"{field_name} must be an object")
    copied = dict(value)
    _validate_json_keys(copied, field_name=field_name)
    try:
        json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ToolValidationError(
            "invalid_json_value",
            f"{field_name} must contain JSON-compatible values",
        ) from error
    return MappingProxyType(copied)


def _validate_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ToolValidationError("invalid_identifier", f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ToolValidationError("invalid_identifier", f"{field_name} cannot be empty")
    return normalized


def _contains_raw_arguments(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key in {
                "arguments",
                "argumentsjson",
                "rawarguments",
                "rawinput",
            }:
                return True
            if _contains_raw_arguments(nested_value):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_raw_arguments(item) for item in value)
    return False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    effect: ToolEffect = ToolEffect.READ_ONLY
    approval_mode: ToolApprovalMode = ToolApprovalMode.AUTO
    timeout_seconds: float = 30.0
    max_output_characters: int = 200_000
    protocol_version: int = 1

    def __post_init__(self) -> None:
        normalized_name = self.name.strip()
        normalized_description = self.description.strip()
        if not _TOOL_NAME.fullmatch(normalized_name):
            raise ToolValidationError(
                "invalid_tool_name",
                "Tool name must be lowercase snake_case and at most 64 characters",
            )
        if not normalized_description or len(normalized_description) > 1_024:
            raise ToolValidationError(
                "invalid_tool_description",
                "Tool description must contain between 1 and 1024 characters",
            )
        schema = _validated_json_object(self.input_schema, field_name="input_schema")
        if schema.get("type") != "object":
            raise ToolValidationError(
                "invalid_input_schema",
                "Tool input schema root type must be object",
            )
        try:
            check_tool_schema(schema)
        except ToolSchemaError as error:
            raise ToolValidationError(
                "invalid_input_schema",
                "Tool input schema must be valid JSON Schema",
            ) from error
        if not isinstance(self.effect, ToolEffect) or not isinstance(
            self.approval_mode,
            ToolApprovalMode,
        ):
            raise ToolValidationError(
                "invalid_tool_policy",
                "Tool effect and approval mode must use protocol enum values",
            )
        if (
            self.effect is not ToolEffect.READ_ONLY
            and self.approval_mode is not ToolApprovalMode.REQUIRED
        ):
            raise ToolValidationError(
                "approval_required",
                "Write and external-action tools must require approval",
            )
        if not 0 < self.timeout_seconds <= 600:
            raise ToolValidationError(
                "invalid_tool_timeout",
                "Tool timeout must be greater than 0 and at most 600 seconds",
            )
        if self.max_output_characters <= 0:
            raise ToolValidationError(
                "invalid_tool_output_limit",
                "Tool output limit must be positive",
            )
        if self.protocol_version != 1:
            raise ToolValidationError(
                "unsupported_tool_protocol",
                "Only tool protocol version 1 is supported",
            )
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "description", normalized_description)
        object.__setattr__(self, "input_schema", schema)


@dataclass(frozen=True)
class ToolCall:
    id: str
    conversation_id: str
    turn_id: str
    response_variant_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    status: ToolCallStatus
    created_at: str

    def __post_init__(self) -> None:
        for field_name in ("id", "conversation_id", "turn_id", "response_variant_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_identifier(getattr(self, field_name), field_name=field_name),
            )
        normalized_tool_name = self.tool_name.strip()
        if not _TOOL_NAME.fullmatch(normalized_tool_name):
            raise ToolValidationError("invalid_tool_name", "Tool call has an invalid name")
        if not isinstance(self.status, ToolCallStatus):
            raise ToolValidationError(
                "invalid_tool_call_status",
                "Tool call status must use a protocol enum value",
            )
        object.__setattr__(self, "tool_name", normalized_tool_name)
        object.__setattr__(
            self,
            "arguments",
            _validated_json_object(self.arguments, field_name="arguments"),
        )
        object.__setattr__(
            self,
            "created_at",
            _validate_identifier(self.created_at, field_name="created_at"),
        )

    @property
    def canonical_arguments_json(self) -> str:
        return json.dumps(
            dict(self.arguments),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    structured_content: Optional[Mapping[str, Any]] = None
    is_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tool_call_id",
            _validate_identifier(self.tool_call_id, field_name="tool_call_id"),
        )
        if not isinstance(self.content, str):
            raise ToolValidationError(
                "invalid_tool_result",
                "Tool result content must be text",
            )
        if not isinstance(self.is_truncated, bool):
            raise ToolValidationError(
                "invalid_tool_result",
                "Tool result truncation flag must be boolean",
            )
        if self.structured_content is not None:
            object.__setattr__(
                self,
                "structured_content",
                _validated_json_object(
                    self.structured_content,
                    field_name="structured_content",
                ),
            )


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tool_call_id: str
    summary: str
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = ""
    resolved_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("id", "tool_call_id", "created_at"):
            object.__setattr__(
                self,
                field_name,
                _validate_identifier(getattr(self, field_name), field_name=field_name),
            )
        summary = self.summary.strip()
        reason = self.reason.strip()
        if not summary or not reason:
            raise ToolValidationError(
                "invalid_approval_copy",
                "Approval summary and reason cannot be empty",
            )
        if len(summary) > 240 or len(reason) > 1_024:
            raise ToolValidationError(
                "invalid_approval_copy",
                "Approval copy exceeds its safe display limit",
            )
        if not isinstance(self.status, ApprovalStatus):
            raise ToolValidationError(
                "invalid_approval_state",
                "Approval status must use a protocol enum value",
            )
        if self.status is ApprovalStatus.PENDING and self.resolved_at is not None:
            raise ToolValidationError(
                "invalid_approval_state",
                "Pending approval cannot have a resolution timestamp",
            )
        if self.status is not ApprovalStatus.PENDING and not self.resolved_at:
            raise ToolValidationError(
                "invalid_approval_state",
                "Resolved approval must have a resolution timestamp",
            )
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "reason", reason)
        metadata = _validated_json_object(self.metadata, field_name="metadata")
        if _contains_raw_arguments(metadata):
            raise ToolValidationError(
                "unsafe_approval_metadata",
                "Approval metadata cannot contain raw tool arguments",
            )
        object.__setattr__(
            self,
            "metadata",
            metadata,
        )


@dataclass(frozen=True)
class ToolApprovalPrompt:
    """Safe, user-facing copy produced by a tool before a side effect."""

    summary: str
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        summary = self.summary.strip()
        reason = self.reason.strip()
        if not summary or not reason:
            raise ToolValidationError(
                "invalid_approval_copy",
                "Approval summary and reason cannot be empty",
            )
        if len(summary) > 240 or len(reason) > 1_024:
            raise ToolValidationError(
                "invalid_approval_copy",
                "Approval copy exceeds its safe display limit",
            )
        metadata = _validated_json_object(self.metadata, field_name="metadata")
        if _contains_raw_arguments(metadata):
            raise ToolValidationError(
                "unsafe_approval_metadata",
                "Approval metadata cannot contain raw tool arguments",
            )
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True)
class ToolActivityCopy:
    """Short, trusted status text shown in the chat surface."""

    running: str
    completed: str
    failed: str
    cancelled: str = "操作已停止"

    def __post_init__(self) -> None:
        for field_name in ("running", "completed", "failed", "cancelled"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ToolValidationError(
                    "invalid_activity_copy",
                    "Tool activity copy must be text",
                )
            normalized = " ".join(value.split())
            if not normalized or len(normalized) > 160:
                raise ToolValidationError(
                    "invalid_activity_copy",
                    "Tool activity copy must contain between 1 and 160 characters",
                )
            object.__setattr__(self, field_name, normalized)
