"""Versioned Agent Platform extension events, decisions and dispatch bus."""

from .bus import (
    EXTENSION_BUS_SCHEMA_VERSION,
    AgentExtension,
    ExtensionRegistration,
    InProcessExtensionBus,
)
from .builtin import (
    ApprovalPolicyExtension,
    AuditExtension,
    OutputSpillExtension,
    SensitiveValueRedactionExtension,
)
from .decisions import merge_post_tool_decisions, merge_pre_tool_decisions
from .protocol import (
    EXTENSION_PROTOCOL_VERSION,
    ExtensionBus,
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionDispatchResult,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
)

__all__ = [
    "EXTENSION_BUS_SCHEMA_VERSION",
    "EXTENSION_PROTOCOL_VERSION",
    "AgentExtension",
    "ApprovalPolicyExtension",
    "AuditExtension",
    "ExtensionBus",
    "ExtensionDecision",
    "ExtensionDecisionKind",
    "ExtensionDispatchResult",
    "ExtensionEvent",
    "ExtensionEventMode",
    "ExtensionHook",
    "ExtensionRegistration",
    "InProcessExtensionBus",
    "OutputSpillExtension",
    "SensitiveValueRedactionExtension",
    "merge_post_tool_decisions",
    "merge_pre_tool_decisions",
]
