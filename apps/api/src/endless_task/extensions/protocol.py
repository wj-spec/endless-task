from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    freeze_json_object,
    require_identifier,
    require_protocol_version,
    require_text,
)
from endless_task.runtime_ledger import TraceContext

EXTENSION_PROTOCOL_VERSION = 1


class ExtensionHook(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BEFORE_CONTEXT = "before_context"
    AFTER_CONTEXT = "after_context"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_TOOL = "before_tool"
    AROUND_TOOL = "around_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_COMPACTION = "before_compaction"
    AFTER_COMPACTION = "after_compaction"
    CHILD_SPAWN = "child_spawn"
    CHILD_COMPLETED = "child_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class ExtensionEventMode(str, Enum):
    OBSERVATION = "observation"
    SAFETY_DECISION = "safety_decision"


class ExtensionDecisionKind(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    REPLACE_ARGUMENTS = "replace_arguments"
    ACCEPT = "accept"
    REPLACE_RESULT = "replace_result"
    BLOCK_RESULT = "block_result"


@dataclass(frozen=True)
class ExtensionEvent:
    event_id: str
    hook: ExtensionHook
    mode: ExtensionEventMode
    trace: TraceContext
    payload: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = EXTENSION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=EXTENSION_PROTOCOL_VERSION,
            protocol="extension_event",
        )
        object.__setattr__(self, "event_id", require_identifier(self.event_id, field_name="event_id"))
        if not isinstance(self.hook, ExtensionHook) or not isinstance(
            self.mode, ExtensionEventMode
        ):
            raise AgentPlatformError("invalid_extension_value", "Extension event enums must use protocol values")
        object.__setattr__(
            self,
            "payload",
            freeze_json_object(self.payload, field_name="payload"),
        )


@dataclass(frozen=True)
class ExtensionDecision:
    extension_id: str
    kind: ExtensionDecisionKind
    reason: str
    replacement: Optional[Mapping[str, Any]] = None
    protocol_version: int = EXTENSION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=EXTENSION_PROTOCOL_VERSION,
            protocol="extension_decision",
        )
        object.__setattr__(
            self,
            "extension_id",
            require_identifier(self.extension_id, field_name="extension_id"),
        )
        if not isinstance(self.kind, ExtensionDecisionKind):
            raise AgentPlatformError("invalid_extension_value", "Extension decision kind must use a protocol enum value")
        object.__setattr__(
            self,
            "reason",
            require_text(self.reason, field_name="reason", max_length=1_024),
        )
        needs_replacement = self.kind in {
            ExtensionDecisionKind.REPLACE_ARGUMENTS,
            ExtensionDecisionKind.REPLACE_RESULT,
        }
        if needs_replacement != (self.replacement is not None):
            raise AgentPlatformError("invalid_extension_value", "Replacement decisions must carry replacement data exclusively")
        if self.replacement is not None:
            object.__setattr__(
                self,
                "replacement",
                freeze_json_object(self.replacement, field_name="replacement"),
            )


@dataclass(frozen=True)
class ExtensionDispatchResult:
    decision: Optional[ExtensionDecision]
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    protocol_version: int = EXTENSION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=EXTENSION_PROTOCOL_VERSION,
            protocol="extension_dispatch_result",
        )


class ExtensionBus(Protocol):
    async def dispatch(self, event: ExtensionEvent) -> ExtensionDispatchResult: ...