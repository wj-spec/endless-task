from __future__ import annotations

import unittest

from endless_task.agent_platform import plain_json
from endless_task.artifacts.read_tool import ReadArtifactTool
from endless_task.files.read_tool import ReadTextFileTool
from endless_task.delegation.legacy_tools import (
    CancelAgentLegacyTool,
    QueryAgentLegacyTool,
    SpawnAgentLegacyTool,
)
from endless_task.runtime_v2.plan_tool import UpdatePlanTool
from endless_task.tool_platform import (
    BUILTIN_LEGACY_TOOL_NAMES,
    BUILTIN_LEGACY_TOOL_POLICIES,
    UNBOUND_WORKSPACE_DENIED_CAPABILITIES,
    ApprovalPolicy,
    IdempotencyPolicy,
    LegacyToolAdapter,
    ToolExecutionMode,
    capability_grant_for_workspace_binding,
)
from endless_task.tooling import (
    ToolApprovalMode as LegacyApprovalMode,
    ToolDefinition as LegacyToolDefinition,
    ToolEffect as LegacyToolEffect,
)
from endless_task.workspace_runtime import WORKSPACE_TOOLS, workspace_tool_filter
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    ListWorkspaceDirTool,
    ReadSkillFileTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.shell_tool import RunShellTool

DELEGATION_TOOL_NAMES = frozenset(
    {"spawn_agent", "query_agent", "cancel_agent"}
)

BUILTIN_TOOL_CLASSES = (
    ReadTextFileTool,
    ReadArtifactTool,
    ReadWorkspaceFileTool,
    ListWorkspaceDirTool,
    ReadSkillFileTool,
    WriteWorkspaceFileTool,
    DeleteWorkspaceFileTool,
    RunShellTool,
    UpdatePlanTool,
)


def real_tool_definition(tool_class):
    return tool_class.definition


def real_names() -> frozenset[str]:
    return frozenset(real_tool_definition(cls).name for cls in BUILTIN_TOOL_CLASSES)


class LegacyToolPolicyTableTest(unittest.TestCase):
    def test_policy_table_covers_exactly_the_builtin_tools(self) -> None:
        policy_names = frozenset(policy.tool_name for policy in BUILTIN_LEGACY_TOOL_POLICIES)
        builtin_names = frozenset(
            real_tool_definition(cls).name for cls in BUILTIN_TOOL_CLASSES
        )
        self.assertEqual(builtin_names | DELEGATION_TOOL_NAMES, policy_names)
        self.assertEqual(12, len(BUILTIN_LEGACY_TOOL_POLICIES))
        self.assertEqual(
            policy_names,
            frozenset(BUILTIN_LEGACY_TOOL_NAMES),
        )

    def test_policy_names_match_the_real_registered_definitions(self) -> None:
        by_class = {
            real_tool_definition(cls).name: cls for cls in BUILTIN_TOOL_CLASSES
        }
        for policy in BUILTIN_LEGACY_TOOL_POLICIES:
            if policy.tool_name in DELEGATION_TOOL_NAMES:
                continue  # delegation tools are instance-defined; their
                # definition/effect is asserted in test_delegation_legacy_tools
            definition = real_tool_definition(by_class[policy.tool_name])
            self.assertEqual(policy.tool_name, definition.name)

    def test_execution_modes_lock_production_intent(self) -> None:
        expected = {
            "read_text_file": ToolExecutionMode.PARALLEL,
            "read_artifact": ToolExecutionMode.PARALLEL,
            "read_workspace_file": ToolExecutionMode.PARALLEL,
            "list_workspace_dir": ToolExecutionMode.PARALLEL,
            "read_skill_file": ToolExecutionMode.PARALLEL,
            "write_workspace_file": ToolExecutionMode.PATH_SCOPED,
            "delete_workspace_file": ToolExecutionMode.PATH_SCOPED,
            "run_shell": ToolExecutionMode.EXCLUSIVE,
            "update_plan": ToolExecutionMode.SEQUENTIAL,
            "spawn_agent": ToolExecutionMode.EXCLUSIVE,
            "query_agent": ToolExecutionMode.PARALLEL,
            "cancel_agent": ToolExecutionMode.EXCLUSIVE,
        }
        for policy in BUILTIN_LEGACY_TOOL_POLICIES:
            if policy.tool_name in DELEGATION_TOOL_NAMES:
                continue
            self.assertEqual(expected[policy.tool_name], policy.execution_mode)

    def test_idempotency_and_capability_audit_invariants(self) -> None:
        by_class = {
            real_tool_definition(cls).name: cls for cls in BUILTIN_TOOL_CLASSES
        }
        for policy in BUILTIN_LEGACY_TOOL_POLICIES:
            if policy.tool_name in DELEGATION_TOOL_NAMES:
                continue
            definition = real_tool_definition(by_class[policy.tool_name])
            read_only = definition.effect is LegacyToolEffect.READ_ONLY
            if read_only:
                self.assertIn(
                    policy.idempotency,
                    (IdempotencyPolicy.SAFE, IdempotencyPolicy.UNKNOWN),
                )
            else:
                # Effectful tools must declare capabilities and never claim
                # unconditional retry safety.
                self.assertTrue(policy.required_capabilities)
                self.assertNotEqual(IdempotencyPolicy.SAFE, policy.idempotency)

    def test_effectful_tool_capabilities_are_precise(self) -> None:
        by_name = {policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES}
        self.assertEqual(
            frozenset({"workspace.write"}),
            by_name["write_workspace_file"].required_capabilities,
        )
        self.assertIn("workspace.delete", by_name["delete_workspace_file"].required_capabilities)
        shell = by_name["run_shell"].required_capabilities
        self.assertIn("process.spawn", shell)
        self.assertIn("process.signal", shell)
        self.assertIn("external.action", shell)
        self.assertIn("workspace.write", shell)
        self.assertEqual(
            frozenset({"workspace.read"}),
            by_name["read_workspace_file"].required_capabilities,
        )
        self.assertEqual(
            frozenset({"workspace.read"}),
            by_name["list_workspace_dir"].required_capabilities,
        )
        self.assertEqual(
            frozenset({"agent.delegate"}),
            by_name["spawn_agent"].required_capabilities,
        )
        self.assertEqual(
            frozenset({"agent.delegate"}),
            by_name["query_agent"].required_capabilities,
        )
        self.assertEqual(
            frozenset({"agent.delegate"}),
            by_name["cancel_agent"].required_capabilities,
        )

    def test_legacy_approval_modes_are_all_represented(self) -> None:
        by_name = {policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES}
        self.assertIn("delete_workspace_file", by_name)
        self.assertIn("run_shell", by_name)
        self.assertIn("write_workspace_file", by_name)


def _approval(definition: LegacyToolDefinition) -> ApprovalPolicy:
    if definition.approval_mode is LegacyApprovalMode.REQUIRED:
        return ApprovalPolicy.REQUIRED
    return ApprovalPolicy.AUTO


class LegacyToolAdapterPolicyTest(unittest.TestCase):
    def test_adapter_applies_audited_policy_for_every_builtin_tool(self) -> None:
        classes_by_name = {
            real_tool_definition(cls).name: cls for cls in BUILTIN_TOOL_CLASSES
        }
        policies_by_name = {
            policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES
        }
        for name, policy in policies_by_name.items():
            if name in DELEGATION_TOOL_NAMES:
                continue  # delegation adapter asserted in test_delegation_legacy_tools
            tool_class = classes_by_name[name]
            definition = real_tool_definition(tool_class)
            adapter = LegacyToolAdapter(tool_class)
            v2 = adapter.definition
            self.assertEqual(name, v2.name)
            self.assertEqual(policy.execution_mode, v2.execution_mode)
            self.assertEqual(policy.idempotency, v2.idempotency)
            self.assertEqual(policy.required_capabilities, v2.required_capabilities)
            self.assertEqual(_approval(definition), v2.approval)
            self.assertEqual(definition.timeout_seconds, v2.timeout_seconds)
            self.assertEqual(definition.max_output_characters, v2.max_output_characters)
            self.assertEqual(plain_json(definition.input_schema), plain_json(v2.input_schema))

    def test_unknown_tool_keeps_conservative_generic_mapping(self) -> None:
        class UnknownTool:
            definition = LegacyToolDefinition(
                name="unknown_local_write",
                description="Unknown tool for fallback coverage.",
                input_schema={"type": "object"},
                effect=LegacyToolEffect.LOCAL_WRITE,
                approval_mode=LegacyApprovalMode.AUTO,
            )

        adapter = LegacyToolAdapter(UnknownTool)
        v2 = adapter.definition
        self.assertEqual(ToolExecutionMode.PARALLEL, v2.execution_mode)
        self.assertEqual(IdempotencyPolicy.UNKNOWN, v2.idempotency)
        self.assertEqual(frozenset({"workspace.write"}), v2.required_capabilities)


class WorkspaceVisibilityParityTest(unittest.TestCase):
    def test_unbound_grant_hides_exactly_the_legacy_workspace_tools(self) -> None:
        allowed = frozenset().union(
            *(policy.required_capabilities for policy in BUILTIN_LEGACY_TOOL_POLICIES)
        )
        granted = capability_grant_for_workspace_binding(False, allowed)
        by_name = {policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES}
        for tool_name, policy in by_name.items():
            if tool_name in DELEGATION_TOOL_NAMES:
                continue  # delegation visibility asserted separately
            visible = policy.required_capabilities <= granted
            self.assertEqual(
                tool_name not in WORKSPACE_TOOLS,
                visible,
                tool_name,
            )

    def test_delegation_tools_need_bound_workspace_with_delegate_grant(self) -> None:
        # M4A production-review gate: delegation children are workspace-bound
        # research runs. Without a bound workspace, agent.delegate is denied,
        # so spawn/query/cancel_agent are hidden from the model surface.
        allowed = frozenset().union(
            *(policy.required_capabilities for policy in BUILTIN_LEGACY_TOOL_POLICIES)
        )
        by_name = {policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES}
        for tool_name in DELEGATION_TOOL_NAMES:
            self.assertEqual(
                frozenset({"agent.delegate"}),
                by_name[tool_name].required_capabilities,
            )
        unbound_grant = capability_grant_for_workspace_binding(False, allowed)
        for tool_name in DELEGATION_TOOL_NAMES:
            self.assertFalse(
                by_name[tool_name].required_capabilities <= unbound_grant,
                tool_name,
            )
        bound_grant = capability_grant_for_workspace_binding(True, allowed)
        for tool_name in DELEGATION_TOOL_NAMES:
            self.assertTrue(
                by_name[tool_name].required_capabilities <= bound_grant,
                tool_name,
            )

    def test_bound_and_unfiltered_keep_every_tool_visible(self) -> None:
        allowed = frozenset().union(
            *(policy.required_capabilities for policy in BUILTIN_LEGACY_TOOL_POLICIES)
        )
        for bound in (True, False):
            granted = capability_grant_for_workspace_binding(bound, allowed)
            visible = {
                policy.tool_name
                for policy in BUILTIN_LEGACY_TOOL_POLICIES
                if policy.required_capabilities <= granted
            }
            if bound:
                self.assertEqual(real_names() | DELEGATION_TOOL_NAMES, visible)
            else:
                self.assertEqual(
                    real_names() - WORKSPACE_TOOLS - DELEGATION_TOOL_NAMES,
                    visible,
                )

    def test_capability_mask_parity_with_legacy_conversation_predicate(self) -> None:
        class FakeResolver:
            def __init__(self, binding: object | None) -> None:
                self._binding = binding

            def resolve_binding(self, conversation_id: str) -> object | None:
                return self._binding

        allowed = frozenset().union(
            *(policy.required_capabilities for policy in BUILTIN_LEGACY_TOOL_POLICIES)
        )
        by_name = {policy.tool_name: policy for policy in BUILTIN_LEGACY_TOOL_POLICIES}
        for binding, label in ((None, "unbound"), (object(), "bound")):
            legacy_filter = workspace_tool_filter(FakeResolver(binding), "conv_1")
            granted = capability_grant_for_workspace_binding(
                binding is not None, allowed
            )
            for tool_name in sorted(real_names()):
                if tool_name in DELEGATION_TOOL_NAMES:
                    continue  # new capability; legacy predicate predates it
                legacy_visible = (
                    legacy_filter(tool_name) if legacy_filter is not None else True
                )
                v2_visible = by_name[tool_name].required_capabilities <= granted
                self.assertEqual(
                    legacy_visible,
                    v2_visible,
                    f"{label}: {tool_name}",
                )

    def test_unbound_mask_contains_only_workspace_and_process_capabilities(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "workspace.read",
                    "workspace.write",
                    "workspace.delete",
                    "process.spawn",
                    "process.signal",
                    "agent.delegate",
                }
            ),
            UNBOUND_WORKSPACE_DENIED_CAPABILITIES,
        )

    def test_path_scoped_tools_carry_a_recognized_path_argument(self) -> None:
        from endless_task.tool_platform import PATH_ARGUMENT_KEYS

        classes_by_name = {
            real_tool_definition(cls).name: cls for cls in BUILTIN_TOOL_CLASSES
        }
        for policy in BUILTIN_LEGACY_TOOL_POLICIES:
            if policy.execution_mode is not ToolExecutionMode.PATH_SCOPED:
                continue
            definition = real_tool_definition(classes_by_name[policy.tool_name])
            properties = definition.input_schema.get("properties", {})
            self.assertTrue(
                set(properties).intersection(PATH_ARGUMENT_KEYS),
                f"{policy.tool_name} must declare a path argument for path-scoped scheduling",
            )


if __name__ == "__main__":
    unittest.main()
