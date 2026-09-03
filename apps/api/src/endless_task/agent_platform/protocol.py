from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, TypeAlias

PROTOCOL_SCHEMA_VERSION = 1
JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)


class AgentPlatformError(ValueError):
    """Versioned protocol failure with persistence-safe details."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = require_identifier(code, field_name="code")
        self.safe_message = require_text(
            safe_message,
            field_name="safe_message",
            max_length=2_048,
        )
        self.retryable = retryable
        self.details = freeze_json_object(details or {}, field_name="details")


class CancellationSignal(Protocol):
    @property
    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait(self) -> None: ...


def require_identifier(value: str, *, field_name: str, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise AgentPlatformError("invalid_identifier", f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise AgentPlatformError(
            "invalid_identifier",
            f"{field_name} must contain between 1 and {max_length} characters",
        )
    return normalized


def require_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise AgentPlatformError("invalid_text", f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise AgentPlatformError(
            "invalid_text",
            f"{field_name} must contain between 1 and {max_length} characters",
        )
    return normalized


def require_protocol_version(actual: int, *, expected: int, protocol: str) -> None:
    if actual != expected:
        raise AgentPlatformError(
            "unsupported_protocol_version",
            f"{protocol} protocol version {actual} is not supported",
            details={"actual": actual, "expected": expected, "protocol": protocol},
        )


def freeze_json(value: Any, *, field_name: str) -> JsonValue:
    frozen = _freeze_json(value, field_name=field_name)
    try:
        json.dumps(plain_json(frozen), ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as error:
        raise AgentPlatformError(
            "invalid_json_value",
            f"{field_name} must contain finite JSON-compatible values",
        ) from error
    return frozen


def freeze_json_object(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise AgentPlatformError("invalid_json_object", f"{field_name} must be an object")
    frozen = freeze_json(value, field_name=field_name)
    if not isinstance(frozen, Mapping):
        raise AgentPlatformError("invalid_json_object", f"{field_name} must be an object")
    return frozen


def _freeze_json(value: Any, *, field_name: str) -> JsonValue:
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise AgentPlatformError(
                    "invalid_json_object",
                    f"{field_name} object keys must be text",
                )
            copied[key] = _freeze_json(nested, field_name=field_name)
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name=field_name) for item in value)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise AgentPlatformError(
        "invalid_json_value",
        f"{field_name} must contain finite JSON-compatible values",
    )


def plain_json(value: JsonValue) -> Any:
    """Return a mutable JSON-compatible projection of a frozen protocol value."""

    if isinstance(value, Mapping):
        return {key: plain_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


@dataclass(frozen=True)
class SafeDiagnostic:
    code: str
    safe_message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROTOCOL_SCHEMA_VERSION,
            protocol="safe_diagnostic",
        )
        object.__setattr__(self, "code", require_identifier(self.code, field_name="code"))
        object.__setattr__(
            self,
            "safe_message",
            require_text(self.safe_message, field_name="safe_message", max_length=2_048),
        )
        if not isinstance(self.retryable, bool):
            raise AgentPlatformError("invalid_diagnostic", "retryable must be boolean")
        object.__setattr__(
            self,
            "details",
            freeze_json_object(self.details, field_name="details"),
        )