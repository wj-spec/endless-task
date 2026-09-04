"""Parent/child Agent execution contracts."""

from .limits import (
    READ_ONLY_FORBIDDEN_CAPABILITIES,
    READ_ONLY_PROFILE_NAMES,
    ChildCapabilityDecision,
    compute_child_capabilities,
)
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
    "READ_ONLY_FORBIDDEN_CAPABILITIES",
    "READ_ONLY_PROFILE_NAMES",
    "ChildCapabilityDecision",
    "compute_child_capabilities",
    "intersect_capabilities",
]