"""AP-107 flip Stage 2: legacy tool surface vs v2 planned surface parity.

TP-5 step 1 dual-run comparison on the SAME registry of the nine production
built-in tools:

- visible name set for bound / unbound conversations must match between the
  legacy conversation predicate path and the v2 capability-granted surface,
- for every tool visible on both sides the provider-facing parameter schema
  must be identical (the v2 projection is a passthrough for the calibrated
  profile).

Test-only harness; no production code is exercised here.
"""

from __future__ import annotations

import unittest

from endless_task.agent_platform import plain_json
from endless_task.artifacts.read_tool import ReadArtifactTool
from endless_task.files.read_tool import ReadTextFileTool
from endless_task.runtime_v2.plan_tool import UpdatePlanTool
from endless_task.tool_platform import (
    CapabilityContext,
    CapabilityProfile,
    InMemoryToolCatalog,
    LegacyToolAdapter,
    ToolProvenance,
    ToolProvenanceKind,
    ToolScope,
    ToolSurfaceRequest,
    builtin_legacy_tool_policy,
    capability_grant_for_workspace_binding,
    create_openai_compatible_profile,
    project_tool_surface,
)
from endless_task.tooling import ToolRegistry
from endless_task.workspace_runtime import WORKSPACE_TOOLS, workspace_tool_filter
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    ListWorkspaceDirTool,
    ReadSkillFileTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.shell_tool import RunShellTool

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


class FakeResolver:
    def __init__(self, binding: object | None) -> None:
        self._binding = binding

    def resolve_binding(self, conversation_id: str) -> object | None:
        return self._binding


class LegacySurfaceParityTest(unittest.TestCase):
    def _legacy_visible_names(self, binding: object | None) -> frozenset[str]:
        registry = ToolRegistry()
        for tool_class in BUILTIN_TOOL_CLASSES:
            registry.register(tool_class)
        predicate = workspace_tool_filter(FakeResolver(binding), "conversation_1")
        return frozenset(
            definition.name
            for definition in registry.definitions()
            if predicate is None or predicate(definition.name)
        )

    def _v2_surface(self, binding: object | None):
        catalog = InMemoryToolCatalog()
        for tool_class in BUILTIN_TOOL_CLASSES:
            adapter = LegacyToolAdapter(tool_class)
            catalog.register(
                adapter,
                scope=ToolScope.BUILTIN,
                provenance=ToolProvenance(
                    kind=ToolProvenanceKind.LEGACY_ADAPTER,
                    source_id="builtin",
                ),
            )
        allowed = frozenset().union(
            *(
                policy.required_capabilities
                for policy in (
                    builtin_legacy_tool_policy(cls.definition.name)
                    for cls in BUILTIN_TOOL_CLASSES
                )
                if policy is not None
            )
        )
        granted = capability_grant_for_workspace_binding(binding is not None, allowed)
        surface = catalog.surface(
            ToolSurfaceRequest(
                context=CapabilityContext(
                    profile=CapabilityProfile(
                        name="flip_parity",
                        allowed_capabilities=granted,
                    ),
                ),
            )
        )
        return surface

    def test_visible_name_sets_match_legacy_for_bound_and_unbound(self) -> None:
        for binding, label in ((None, "unbound"), (object(), "bound")):
            with self.subTest(binding=label):
                legacy_names = self._legacy_visible_names(binding)
                v2_names = frozenset(
                    registration.definition.name
                    for registration in self._v2_surface(binding).registrations
                )
                self.assertEqual(legacy_names, v2_names, label)
                if binding is None:
                    self.assertEqual(
                        frozenset(
                            cls.definition.name for cls in BUILTIN_TOOL_CLASSES
                        )
                        - WORKSPACE_TOOLS,
                        v2_names,
                    )

    def test_parameter_schemas_are_identical_on_both_sides(self) -> None:
        for binding, label in ((None, "unbound"), (object(), "bound")):
            with self.subTest(binding=label):
                surface = self._v2_surface(binding)
                report = project_tool_surface(
                    surface,
                    create_openai_compatible_profile(),
                )
                self.assertEqual((), report.excluded, label)
                v2_schemas = {
                    projected.name: dict(projected.input_schema)
                    for projected in report.projected
                }
                registry = ToolRegistry()
                for tool_class in BUILTIN_TOOL_CLASSES:
                    registry.register(tool_class)
                predicate = workspace_tool_filter(FakeResolver(binding), "conversation_1")
                for definition in registry.definitions():
                    visible = predicate is None or predicate(definition.name)
                    if not visible:
                        continue
                    self.assertEqual(
                        plain_json(definition.input_schema),
                        v2_schemas[definition.name],
                        definition.name,
                    )


if __name__ == "__main__":
    unittest.main()
