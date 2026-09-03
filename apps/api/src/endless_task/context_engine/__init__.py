"""Context lifecycle contracts and legacy projection adapter."""

from .compatibility import LegacyContextEngineAdapter
from .conformance import assert_context_parity
from .protocol import (
    CONTEXT_PROTOCOL_VERSION,
    BootstrapRequest,
    CommitResult,
    CompactionRequest,
    CompactionResult,
    ContextBudget,
    ContextEngine,
    ContextInput,
    ContextInputKind,
    ContextRequest,
    ContextSegment,
    ContextSegmentKind,
    ContextSnapshot,
    ContextTransform,
    MaintenanceRequest,
    MaintenanceResult,
    TurnOutcome,
)

__all__ = [
    "CONTEXT_PROTOCOL_VERSION",
    "BootstrapRequest",
    "CommitResult",
    "CompactionRequest",
    "CompactionResult",
    "ContextBudget",
    "ContextEngine",
    "ContextInput",
    "ContextInputKind",
    "ContextRequest",
    "ContextSegment",
    "ContextSegmentKind",
    "ContextSnapshot",
    "ContextTransform",
    "LegacyContextEngineAdapter",
    "MaintenanceRequest",
    "MaintenanceResult",
    "TurnOutcome",
    "assert_context_parity",
]