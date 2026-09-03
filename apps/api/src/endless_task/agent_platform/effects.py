from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .protocol import (
    PROTOCOL_SCHEMA_VERSION,
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)


class EffectOutcome(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class EffectReceiptRef:
    effect_id: str
    outcome: EffectOutcome
    schema_version: int = PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROTOCOL_SCHEMA_VERSION,
            protocol="effect_receipt_ref",
        )
        object.__setattr__(
            self,
            "effect_id",
            require_identifier(self.effect_id, field_name="effect_id"),
        )
        if not isinstance(self.outcome, EffectOutcome):
            raise AgentPlatformError(
                "invalid_effect_outcome",
                "Effect outcome must use a protocol enum value",
            )


@dataclass(frozen=True)
class EffectReceipt:
    effect_id: str
    tool_call_id: str
    effect_type: str
    target: str
    started_at: str
    outcome: EffectOutcome
    backend: str
    safe_summary: str
    committed_at: Optional[str] = None
    before_ref: Optional[str] = None
    after_ref: Optional[str] = None
    idempotency_key: Optional[str] = None
    schema_version: int = PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROTOCOL_SCHEMA_VERSION,
            protocol="effect_receipt",
        )
        for field_name in (
            "effect_id",
            "tool_call_id",
            "effect_type",
            "target",
            "started_at",
            "backend",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "safe_summary",
            require_text(self.safe_summary, field_name="safe_summary", max_length=1_024),
        )
        if not isinstance(self.outcome, EffectOutcome):
            raise AgentPlatformError(
                "invalid_effect_outcome",
                "Effect outcome must use a protocol enum value",
            )
        for field_name in ("committed_at", "before_ref", "after_ref", "idempotency_key"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name=field_name, max_length=2_048),
                )
        if self.outcome is EffectOutcome.COMMITTED and self.committed_at is None:
            raise AgentPlatformError(
                "invalid_effect_receipt",
                "Committed effects require committed_at",
            )
        if self.outcome is not EffectOutcome.COMMITTED and self.committed_at is not None:
            raise AgentPlatformError(
                "invalid_effect_receipt",
                "Only committed effects may carry committed_at",
            )

    @property
    def ref(self) -> EffectReceiptRef:
        return EffectReceiptRef(effect_id=self.effect_id, outcome=self.outcome)