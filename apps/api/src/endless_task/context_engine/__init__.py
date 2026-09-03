"""Context lifecycle contracts and legacy projection adapter."""

from .compatibility import LegacyContextEngineAdapter
from .conformance import assert_context_parity
from .retention import (
    DEFAULT_MAX_RESULT_SHARE,
    RETENTION_SCHEMA_VERSION,
    ToolResultRetention,
    plan_tool_result_retention,
)
from .planner import (
    CONTEXT_PLANNER_SCHEMA_VERSION,
    ContextPlan,
    estimate_text_tokens,
    fingerprint_context,
    plan_context_segments,
)
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
    "CONTEXT_PLANNER_SCHEMA_VERSION",
    "CONTEXT_PROTOCOL_VERSION",
    "BootstrapRequest",
    "CommitResult",
    "CompactionRequest",
    "CompactionResult",
    "ContextBudget",
    "ContextEngine",
    "ContextInput",
    "ContextInputKind",
    "ContextPlan",
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
    "DEFAULT_MAX_RESULT_SHARE",
    "RETENTION_SCHEMA_VERSION",
    "ToolResultRetention",
    "estimate_text_tokens",
    "fingerprint_context",
    "plan_context_segments",
    "plan_tool_result_retention",
]