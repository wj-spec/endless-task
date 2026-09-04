"""Parent/child Agent execution contracts."""

from .coordinator import (
    MAX_DEPTH,
    READ_ONLY_WORKSPACE_MODES,
    ChildRunRecord,
    ChildRunStatus,
    InProcessChildCoordinator,
    _assemble_child_prompt,
)
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
from .result_projection import (
    CHILD_DELIVERABLE_MAX_CHARACTERS,
    CHILD_SUMMARY_MAX_CHARACTERS,
    ChildResultPolicy,
    project_child_outcome,
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
    "CHILD_DELIVERABLE_MAX_CHARACTERS",
    "CHILD_SUMMARY_MAX_CHARACTERS",
    "ChildResultPolicy",
    "project_child_outcome",
    "MAX_DEPTH",
    "READ_ONLY_WORKSPACE_MODES",
    "ChildRunRecord",
    "ChildRunStatus",
    "InProcessChildCoordinator",
    "_assemble_child_prompt",
]
