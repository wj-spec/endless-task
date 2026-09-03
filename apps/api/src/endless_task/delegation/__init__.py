"""Parent/child Agent execution contracts."""

from .protocol import (
    DELEGATION_PROTOCOL_VERSION,
    ArtifactRef,
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutcome,
    ChildOutputSchema,
    ChildStatus,
    DelegationRuntime,
    Finding,
    SpawnSpec,
    UsageSummary,
    WorkspaceMode,
    intersect_capabilities,
)

__all__ = [
    "DELEGATION_PROTOCOL_VERSION",
    "ArtifactRef",
    "ChildContextPolicy",
    "ChildModelPolicy",
    "ChildOutcome",
    "ChildOutputSchema",
    "ChildStatus",
    "DelegationRuntime",
    "Finding",
    "SpawnSpec",
    "UsageSummary",
    "WorkspaceMode",
    "intersect_capabilities",
]