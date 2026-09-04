"""Child delegation limits and capability computation (M4A DR-0 slice 1).

Pure checks that turn a validated :class:`SpawnSpec` into an effective child
capability set without ever widening the parent:

- effective = parent & requested & profile & backend & deployment,
- a capability the parent does not hold can never be requested (denied
  escalation fails closed),
- a read-only child profile must not receive write/process/external
  capabilities (M4A gate: children cannot escalate; read-only first).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from endless_task.agent_platform import AgentPlatformError

from .protocol import SpawnSpec, intersect_capabilities

#: Capabilities that contradict a read-only delegation.
READ_ONLY_FORBIDDEN_CAPABILITIES = frozenset(
    {
        "workspace.write",
        "workspace.delete",
        "process.spawn",
        "process.signal",
        "external.action",
    }
)

#: Built-in profile names that must stay read-only (tool_platform vocabulary).
READ_ONLY_PROFILE_NAMES = frozenset({"subagent_readonly"})


@dataclass(frozen=True)
class ChildCapabilityDecision:
    effective: frozenset[str]
    read_only: bool
    denied: frozenset[str]


def compute_child_capabilities(
    spec: SpawnSpec,
    *,
    parent_capabilities: frozenset[str],
    profile_capabilities: Optional[frozenset[str]] = None,
    backend_capabilities: Optional[frozenset[str]] = None,
    deployment_capabilities: Optional[frozenset[str]] = None,
) -> ChildCapabilityDecision:
    """Effective child capabilities; fails closed on any escalation."""
    if not isinstance(spec, SpawnSpec):
        raise AgentPlatformError(
            "invalid_delegation_value",
            "Child capability computation requires a SpawnSpec",
        )
    if not isinstance(parent_capabilities, frozenset):
        raise AgentPlatformError(
            "invalid_delegation_value",
            "parent_capabilities must be a frozenset",
        )
    requested = frozenset(spec.requested_capabilities)
    denied = requested - parent_capabilities
    if denied:
        raise AgentPlatformError(
            "delegation_capability_escalation",
            "子 Agent 请求了父级未持有的能力，已拒绝。",
            retryable=False,
            details={"denied": sorted(denied)},
        )
    profile = frozenset(profile_capabilities) if profile_capabilities is not None else requested
    effective = intersect_capabilities(
        parent=parent_capabilities,
        requested=requested,
        profile=profile,
        backend=(
            frozenset(backend_capabilities)
            if backend_capabilities is not None
            else requested
        ),
        deployment=(
            frozenset(deployment_capabilities)
            if deployment_capabilities is not None
            else requested
        ),
    )
    read_only = (
        spec.capability_profile in READ_ONLY_PROFILE_NAMES
        or not (effective & READ_ONLY_FORBIDDEN_CAPABILITIES)
    )
    if spec.capability_profile in READ_ONLY_PROFILE_NAMES:
        violation = effective & READ_ONLY_FORBIDDEN_CAPABILITIES
        if violation:
            raise AgentPlatformError(
                "delegation_read_only_violation",
                "只读子 Agent 不能获得写/进程/外部能力。",
                retryable=False,
                details={"violation": sorted(violation)},
            )
    return ChildCapabilityDecision(
        effective=effective,
        read_only=read_only,
        denied=frozenset(),
    )


__all__ = [
    "READ_ONLY_FORBIDDEN_CAPABILITIES",
    "READ_ONLY_PROFILE_NAMES",
    "ChildCapabilityDecision",
    "compute_child_capabilities",
]
