"""M4A DR-0 slice 1: child delegation capability limits."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    SpawnSpec,
    WorkspaceMode,
    compute_child_capabilities,
)


def spec(
    *,
    capabilities: frozenset[str] = frozenset(),
    profile: str = "subagent_readonly",
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY_SHARED,
) -> SpawnSpec:
    return SpawnSpec(
        task="调查",
        expected_output=ChildOutputSchema(
            name="research_output",
            schema={"type": "object"},
        ),
        capability_profile=profile,
        requested_capabilities=frozenset(capabilities),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
        context_policy=ChildContextPolicy(),
        workspace_mode=workspace_mode,
        timeout_seconds=120.0,
        parent_run_id="parent_1",
        parent_tool_call_id="call_1",
    )


#: Parent run may hold read + write + memory + query but NOT process/external.
PARENT = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "memory.read",
        "session.query",
    }
)


class ChildCapabilityLimitsTest(unittest.TestCase):
    def test_read_only_child_effective_is_intersection(self) -> None:
        decision = compute_child_capabilities(
            spec(capabilities={"workspace.read", "session.query"}),
            parent_capabilities=PARENT,
        )
        self.assertTrue(decision.read_only)
        self.assertEqual(
            frozenset({"workspace.read", "session.query"}),
            decision.effective,
        )
        self.assertEqual(frozenset(), decision.denied)

    def test_requested_escalation_fails_closed(self) -> None:
        # Parent does NOT hold external.action -> denial must fail closed.
        with self.assertRaises(AgentPlatformError) as caught:
            compute_child_capabilities(
                spec(capabilities={"workspace.read", "external.action"}),
                parent_capabilities=PARENT,
            )
        self.assertEqual("delegation_capability_escalation", caught.exception.code)

    def test_read_only_profile_rejects_write_capability(self) -> None:
        # Parent holds workspace.write, but a read-only child must not get it.
        with self.assertRaises(AgentPlatformError) as caught:
            compute_child_capabilities(
                spec(capabilities={"workspace.write"}),
                parent_capabilities=PARENT,
            )
        self.assertEqual("delegation_read_only_violation", caught.exception.code)

    def test_read_only_child_denied_escalation_wins_over_violation(self) -> None:
        # process.spawn is neither held by the parent nor allowed read-only.
        with self.assertRaises(AgentPlatformError) as caught:
            compute_child_capabilities(
                spec(capabilities={"process.spawn"}),
                parent_capabilities=PARENT,
            )
        self.assertEqual("delegation_capability_escalation", caught.exception.code)

    def test_work_profile_child_can_hold_write_when_parent_allows(self) -> None:
        decision = compute_child_capabilities(
            spec(
                capabilities={"workspace.read", "workspace.write"},
                profile="subagent_workspace",
                workspace_mode=WorkspaceMode.SHARED_WRITE_SERIALIZED,
            ),
            parent_capabilities=PARENT,
        )
        self.assertFalse(decision.read_only)
        self.assertIn("workspace.write", decision.effective)

    def test_profile_capabilities_narrow_effective(self) -> None:
        decision = compute_child_capabilities(
            spec(capabilities={"workspace.read", "workspace.write"}),
            parent_capabilities=PARENT,
            profile_capabilities=frozenset({"workspace.read"}),
        )
        self.assertEqual(frozenset({"workspace.read"}), decision.effective)
        self.assertNotIn("workspace.write", decision.effective)

    def test_backend_and_deployment_narrow_effective(self) -> None:
        decision = compute_child_capabilities(
            spec(capabilities={"workspace.read", "memory.read", "session.query"}),
            parent_capabilities=PARENT,
            backend_capabilities=frozenset({"workspace.read", "memory.read"}),
            deployment_capabilities=frozenset({"workspace.read"}),
        )
        self.assertEqual(frozenset({"workspace.read"}), decision.effective)

    def test_requested_empty_yields_empty_effective(self) -> None:
        decision = compute_child_capabilities(
            spec(capabilities=frozenset()),
            parent_capabilities=PARENT,
        )
        self.assertEqual(frozenset(), decision.effective)
        self.assertTrue(decision.read_only)

    def test_non_spec_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            compute_child_capabilities(
                spec(capabilities={"workspace.read"}),  # type: ignore[arg-type]
                parent_capabilities="not-a-frozenset",  # type: ignore[arg-type]
            )
        # Parent type check fails before any capability work is done.
        self.assertEqual("invalid_delegation_value", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
