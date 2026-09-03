"""Shared, implementation-neutral contracts for Agent Platform modules."""

from .effects import EffectOutcome, EffectReceipt, EffectReceiptRef
from .protocol import (
    PROTOCOL_SCHEMA_VERSION,
    AgentPlatformError,
    CancellationSignal,
    JsonValue,
    SafeDiagnostic,
    freeze_json,
    freeze_json_object,
    plain_json,
    require_identifier,
    require_protocol_version,
    require_text,
)

__all__ = [
    "PROTOCOL_SCHEMA_VERSION",
    "AgentPlatformError",
    "CancellationSignal",
    "EffectOutcome",
    "EffectReceipt",
    "EffectReceiptRef",
    "JsonValue",
    "SafeDiagnostic",
    "freeze_json",
    "freeze_json_object",
    "plain_json",
    "require_identifier",
    "require_protocol_version",
    "require_text",
]