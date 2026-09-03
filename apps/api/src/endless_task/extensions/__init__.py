"""Versioned Agent Platform extension events and decisions."""

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
    "EXTENSION_PROTOCOL_VERSION",
    "ExtensionBus",
    "ExtensionDecision",
    "ExtensionDecisionKind",
    "ExtensionDispatchResult",
    "ExtensionEvent",
    "ExtensionEventMode",
    "ExtensionHook",
    "merge_post_tool_decisions",
    "merge_pre_tool_decisions",
]