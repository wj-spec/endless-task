from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Mapping, Optional, Protocol, TypeAlias, TypeVar, cast, runtime_checkable

from endless_task.runtime.cancellation import CancellationToken

from .schema import ToolSchemaError, check_tool_schema


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ArgumentValue = TypeVar("_ArgumentValue")
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | Mapping[str, "JsonValue"]


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
    """Legacy sync progress callback accepted by existing long-running tools."""

    def __call__(self, message: str, percent: Optional[float] = None) -> None:
        ...


class ToolValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        path: Optional[str] = None,
        keyword: Optional[str] = None,
        expected: JsonValue = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.path = path
        self.keyword = keyword
        self.expected = expected


@dataclass(frozen=True)
class ToolCallError:
    """Normalized tool failure metadata safe for persistence and model feedback."""

    code: str
    safe_message: str
    retryable: bool
    correlation_id: Optional[str] = None
    path: Optional[str] = None
    keyword: Optional[str] = None
    expected: JsonValue = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not isinstance(self.safe_message, str):
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error code and safe message must be text",
            )
        code = self.code.strip()
        safe_message = self.safe_message.strip()
        if not code or not safe_message:
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error code and safe message cannot be empty",
            )
        if not isinstance(self.retryable, bool):
            raise ToolValidationError(
                "invalid_tool_error",
                "Tool error retryable flag must be boolean",
            )
        for field_name in ("correlation_id", "path", "keyword"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ToolValidationError(
                    "invalid_tool_error",
                    f"Tool error {field_name} must be non-empty text when provided",
                )
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.strip())
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "safe_message", safe_message)
        object.__setattr__(
            self,
            "expected",
            _validated_json_value(self.expected, field_name="expected"),
        )

    @property
    def details(self) -> Mapping[str, JsonValue]:
        details: dict[str, JsonValue] = {}
        if self.path is not None:
            details["path"] = self.path
        if self.keyword is not None:
            details["keyword"] = self.keyword
        if self.expected is not None:
            details["expected"] = self.expected
        return MappingProxyType(details)


class ToolError(Exception):
    """Exception wrapper for a normalized tool failure."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool,
        correlation_id: Optional[str] = None,
        path: Optional[str] = None,
        keyword: Optional[str] = None,
        expected: JsonValue = None,
    ) -> None:
        failure = ToolCallError(
            code=code,
            safe_message=safe_message,
            retryable=retryable,
            correlation_id=correlation_id,
            path=path,
            keyword=keyword,
            expected=expected,
        )
        super().__init__(failure.safe_message)
        self.failure = failure
        self.code = failure.code
        self.safe_message = failure.safe_message
        self.retryable = failure.retryable
        self.correlation_id = failure.correlation_id
        self.path = failure.path
        self.keyword = failure.keyword
        self.expected = failure.expected


def _plain_json_value(value: Any, *, field_name: str) -> JsonValue:
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ToolValidationError(
                    "invalid_json_object",
                    f"{field_name} object keys must be text",
                )
            copied[key] = _plain_json_value(
                nested_value,
                field_name=field_name,
            )
        return copied
    if isinstance(value, (list, tuple)):
        return [
            _plain_json_value(item, field_name=field_name)
            for item in value
        ]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ToolValidationError(
        "invalid_json_value",
        f"{field_name} must contain JSON-compatible values",
    )


def _validated_json_value(value: Any, *, field_name: str) -> JsonValue:
    try:
        copied = _plain_json_value(value, field_name=field_name)
        json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except RecursionError as error:
        raise ToolValidationError(
            "invalid_json_value",
            f"{field_name} must not contain recursive values",
        ) from error
    except (TypeError, ValueError) as error:
        raise ToolValidationError(
            "invalid_json_value",
            f"{field_name} must contain JSON-compatible values",
        ) from error
    if isinstance(copied, dict):
        return MappingProxyType(copied)
    return copied


def _validated_json_object(
    value: Mapping[str, Any],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolValidationError("invalid_json_object", f"{field_name} must be an object")
    validated = _validated_json_value(value, field_name=field_name)
    if not isinstance(validated, Mapping):
        raise ToolValidationError("invalid_json_object", f"{field_name} must be an object")
    return validated


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
class ToolProgressEvent:
    """Typed progress payload emitted by long-running tools."""

    message: str
    percent: Optional[float] = None

    def __post_init__(self) -> None:
        message = self.message.strip() if isinstance(self.message, str) else ""
        if not message:
            raise ToolValidationError(
                "invalid_tool_progress",
                "Tool progress message cannot be empty",
            )
        object.__setattr__(self, "message", message)
        if self.percent is None:
            return
        if isinstance(self.percent, bool) or not isinstance(self.percent, (int, float)):
            raise ToolValidationError(
                "invalid_tool_progress",
                "Tool progress percent must be numeric when provided",
            )
        percent = float(self.percent)
        if not math.isfinite(percent):
            raise ToolValidationError(
                "invalid_tool_progress",
                "Tool progress percent must be finite",
            )
        object.__setattr__(self, "percent", percent)

    @property
    def payload(self) -> Mapping[str, JsonValue]:
        return MappingProxyType({"message": self.message, "percent": self.percent})


class ToolProgressReporter(Protocol):
    """Async sink for typed tool progress events."""

    async def __call__(self, event: ToolProgressEvent) -> None:
        ...


@dataclass(frozen=True)
class ToolExecutionContext:
    """Shared execution context for a single tool call."""

    cancellation_token: CancellationToken
    deadline_monotonic: Optional[float] = None
    correlation_id: Optional[str] = None
    progress_reporter: Optional[ToolProgressReporter] = None

    def __post_init__(self) -> None:
        if not isinstance(self.cancellation_token, CancellationToken):
            raise ToolValidationError(
                "invalid_tool_context",
                "Tool execution context requires a cancellation token",
            )
        if self.deadline_monotonic is not None:
            if isinstance(self.deadline_monotonic, bool) or not isinstance(
                self.deadline_monotonic,
                (int, float),
            ):
                raise ToolValidationError(
                    "invalid_tool_context",
                    "Tool execution deadline must be numeric when provided",
                )
            deadline = float(self.deadline_monotonic)
            if not math.isfinite(deadline):
                raise ToolValidationError(
                    "invalid_tool_context",
                    "Tool execution deadline must be finite",
                )
            object.__setattr__(self, "deadline_monotonic", deadline)
        if self.correlation_id is not None:
            object.__setattr__(
                self,
                "correlation_id",
                _validate_identifier(self.correlation_id, field_name="correlation_id"),
            )
        if self.progress_reporter is not None and not callable(self.progress_reporter):
            raise ToolValidationError(
                "invalid_tool_context",
                "Tool progress reporter must be callable",
            )

    async def report_progress(
        self,
        event: ToolProgressEvent | str,
        percent: Optional[float] = None,
    ) -> None:
        if self.progress_reporter is None:
            return
        progress_event = (
            event if isinstance(event, ToolProgressEvent) else ToolProgressEvent(event, percent)
        )
        await self.progress_reporter(progress_event)

    def raise_if_cancelled(self) -> None:
        self.cancellation_token.raise_if_cancelled()


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
            and self.effect is ToolEffect.EXTERNAL_ACTION
            and self.approval_mode is not ToolApprovalMode.REQUIRED
        ):
            raise ToolValidationError(
                "approval_required",
                "External-action tools must require approval",
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

    def require_argument(
        self,
        name: str,
        expected_type: type[_ArgumentValue],
    ) -> _ArgumentValue:
        if name not in self.arguments:
            raise ToolValidationError(
                "invalid_tool_arguments",
                f"Tool argument {name} is required",
            )
        return self._typed_argument(name, self.arguments[name], expected_type)

    def optional_argument(
        self,
        name: str,
        expected_type: type[_ArgumentValue],
        default: _ArgumentValue,
    ) -> _ArgumentValue:
        if name not in self.arguments:
            return default
        return self._typed_argument(name, self.arguments[name], expected_type)

    @staticmethod
    def _typed_argument(
        name: str,
        value: Any,
        expected_type: type[_ArgumentValue],
    ) -> _ArgumentValue:
        matches = (
            type(value) is int
            if expected_type is int
            else isinstance(value, expected_type)
        )
        if not matches:
            raise ToolValidationError(
                "invalid_tool_arguments",
                f"Tool argument {name} has an invalid type",
            )
        return cast(_ArgumentValue, value)

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
    structured_content: JsonValue = None
    is_truncated: bool = False
    # 工具可请求「提前终止」agent 循环（对应 pi executeToolCalls 的 terminate）。
    # 仅当本批所有工具结果都 terminate 时才终止；默认 False，读写工具不设置。
    terminate: bool = False
    error: Optional[ToolCallError] = None

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
        if not isinstance(self.terminate, bool):
            raise ToolValidationError(
                "invalid_tool_result",
                "Tool result terminate flag must be boolean",
            )
        if self.error is not None and not isinstance(self.error, ToolCallError):
            raise ToolValidationError(
                "invalid_tool_result",
                "Tool result error must use the normalized error contract",
            )
        if self.error is not None and self.terminate:
            raise ToolValidationError(
                "invalid_tool_result",
                "Failed tool results cannot terminate the agent loop",
            )
        object.__setattr__(
            self,
            "structured_content",
            _validated_json_value(
                self.structured_content,
                field_name="structured_content",
            ),
        )

    @classmethod
    def failed(
        cls,
        *,
        tool_call_id: str,
        error: ToolCallError,
    ) -> "ToolResult":
        return cls(
            tool_call_id=tool_call_id,
            content=error.safe_message,
            error=error,
        )


@runtime_checkable
class ContextualTool(Protocol):
    """Tool capability that receives the full execution context explicitly."""

    async def execute_with_context(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...


@runtime_checkable
class ProgressReportingTool(Protocol):
    """Legacy progress-capable tool entrypoint kept during migration."""

    async def execute_with_progress(
        self,
        call: ToolCall,
        token: CancellationToken,
        *,
        on_progress: ProgressReporter,
    ) -> ToolResult:
        ...


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
