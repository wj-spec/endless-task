"""AP-105b: atomic MCP namespace replacement on the in-memory catalog."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.tool_platform import (
    CapabilityContext,
    CapabilityProfile,
    FakeTool,
    InMemoryToolCatalog,
    ToolDefinitionV2,
    ToolEffect,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolProvenance,
    ToolProvenanceKind,
    ToolScope,
    ToolSurfaceRequest,
)

SERVER_A = "mcp_server_a"
SERVER_B = "mcp_server_b"

WORK_PROFILE = CapabilityProfile(
    name="mcp_work",
    allowed_capabilities=frozenset(
        {
            "workspace.read",
            "workspace.write",
            "external.action",
            "session.query",
            "memory.read",
            "network.outbound",
            "process.spawn",
            "process.signal",
            "workspace.delete",
        }
    ),
)


def mcp_tool(
    name: str,
    *,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    capabilities: frozenset[str] = frozenset(),
    description: str | None = None,
) -> FakeTool:
    return FakeTool(
        definition=ToolDefinitionV2(
            name=name,
            description=description or f"Use {name}.",
            input_schema={"type": "object"},
            effect=effect,
            required_capabilities=capabilities,
        ),
        outcome=ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
        ),
    )


def mcp_provenance(source_id: str, revision: str | None = None) -> ToolProvenance:
    return ToolProvenance(
        kind=ToolProvenanceKind.MCP,
        source_id=source_id,
        revision=revision,
    )


def context(workspace_id: str = SERVER_A) -> CapabilityContext:
    return CapabilityContext(profile=WORK_PROFILE, workspace_id=workspace_id)


class McpCatalogReplacementTest(unittest.TestCase):
    def _catalog_with(self, server_a_tools: tuple = ()) -> InMemoryToolCatalog:
        catalog = InMemoryToolCatalog()
        for tool in server_a_tools:
            catalog.register(
                tool,
                scope=ToolScope.WORKSPACE,
                scope_id=SERVER_A,
                provenance=mcp_provenance(SERVER_A, revision="v1"),
            )
        return catalog

    def test_replace_registers_all_tools_in_one_generation(self) -> None:
        catalog = self._catalog_with(
            (mcp_tool("mcp__server_a__old"),),
        )
        added = catalog.replace_provenance(
            kind=ToolProvenanceKind.MCP,
            source_id=SERVER_A,
            tools=(
                mcp_tool("mcp__server_a__echo"),
                mcp_tool("mcp__server_a__search"),
            ),
            scope=ToolScope.WORKSPACE,
            scope_id=SERVER_A,
            revision="v2",
        )
        self.assertEqual(2, len(added))
        self.assertEqual(2, catalog.generation)
        names = {
            registration.definition.name
            for registration in catalog.registrations()
        }
        self.assertEqual(
            {"mcp__server_a__echo", "mcp__server_a__search"},
            names,
        )
        for registration in catalog.registrations():
            self.assertEqual(SERVER_A, registration.provenance.source_id)
            self.assertEqual("v2", registration.provenance.revision)
        with self.assertRaises(AgentPlatformError):
            catalog.resolve("mcp__server_a__old", context())

    def test_second_replacement_overrides_first_with_replacement_chain(self) -> None:
        catalog = self._catalog_with(
            (mcp_tool("mcp__server_a__tool"),),
        )
        first = catalog.replace_provenance(
            kind=ToolProvenanceKind.MCP,
            source_id=SERVER_A,
            tools=(mcp_tool("mcp__server_a__tool"),),
            scope=ToolScope.WORKSPACE,
            scope_id=SERVER_A,
            revision="v2",
        )
        second = catalog.replace_provenance(
            kind=ToolProvenanceKind.MCP,
            source_id=SERVER_A,
            tools=(mcp_tool("mcp__server_a__tool"),),
            scope=ToolScope.WORKSPACE,
            scope_id=SERVER_A,
            revision="v3",
        )
        # Same slot (name+scope): the replacement chain links registrations.
        self.assertEqual(first[0].registration_id, second[0].replaced_registration_id)
        self.assertEqual("v3", second[0].provenance.revision)
        self.assertEqual(3, catalog.generation)
        self.assertEqual(
            ["mcp__server_a__tool"],
            [reg.definition.name for reg in catalog.registrations()],
        )
        # History keeps the full replacement trail.
        history_revisions = [
            reg.provenance.revision for reg in catalog.history()
        ]
        self.assertEqual(["v1", "v2", "v3"], history_revisions)

    def test_renamed_tool_is_a_new_registration_without_replaced_link(self) -> None:
        catalog = self._catalog_with(
            (mcp_tool("mcp__server_a__old_name"),),
        )
        replacement = catalog.replace_provenance(
            kind=ToolProvenanceKind.MCP,
            source_id=SERVER_A,
            tools=(mcp_tool("mcp__server_a__new_name"),),
            scope=ToolScope.WORKSPACE,
            scope_id=SERVER_A,
        )
        self.assertIsNone(replacement[0].replaced_registration_id)
        self.assertEqual(
            ["mcp__server_a__new_name"],
            [reg.definition.name for reg in catalog.registrations()],
        )

    def test_surface_after_replacement_exposes_only_new_generation(self) -> None:
        catalog = self._catalog_with(
            (mcp_tool("mcp__server_a__old"),),
        )
        catalog.replace_provenance(
            kind=ToolProvenanceKind.MCP,
            source_id=SERVER_A,
            tools=(mcp_tool("mcp__server_a__fresh"),),
            scope=ToolScope.WORKSPACE,
            scope_id=SERVER_A,
        )
        surface = catalog.surface(ToolSurfaceRequest(context=context()))
        self.assertEqual(
            ["mcp__server_a__fresh"],
            [registration.definition.name for registration in surface.registrations],
        )

    def test_invalid_replacement_is_atomic_and_leaves_state_untouched(self) -> None:
        catalog = self._catalog_with(
            (mcp_tool("mcp__server_a__keep"),),
        )
        generation_before = catalog.generation
        # Effectful tool without a required capability must fail validation.
        with self.assertRaises(AgentPlatformError):
            catalog.replace_provenance(
                kind=ToolProvenanceKind.MCP,
                source_id=SERVER_A,
                tools=(
                    mcp_tool("mcp__server_a__bad_effect", effect=ToolEffect.LOCAL_WRITE),
                ),
                scope=ToolScope.WORKSPACE,
                scope_id=SERVER_A,
            )
        self.assertEqual(generation_before, catalog.generation)
        self.assertEqual(
            ["mcp__server_a__keep"],
            [reg.definition.name for reg in catalog.registrations()],
        )

    def test_builtin_name_collision_from_namespace_fails_closed(self) -> None:
        catalog = InMemoryToolCatalog()
        builtin = mcp_tool("read_workspace_file")
        catalog.register(
            builtin,
            scope=ToolScope.BUILTIN,
            provenance=ToolProvenance(
                kind=ToolProvenanceKind.BUILTIN,
                source_id="core",
            ),
        )
        with self.assertRaises(AgentPlatformError):
            catalog.replace_provenance(
                kind=ToolProvenanceKind.MCP,
                source_id=SERVER_A,
                tools=(mcp_tool("read_workspace_file"),),
                scope=ToolScope.WORKSPACE,
                scope_id=SERVER_A,
            )
        # Builtin tool is untouched.
        self.assertEqual(
            "read_workspace_file",
            catalog.resolve("read_workspace_file", context()).definition.name,
        )

    def test_namespace_replacement_validation(self) -> None:
        catalog = InMemoryToolCatalog()
        with self.assertRaises(AgentPlatformError):
            catalog.replace_provenance(
                kind="mcp",
                source_id=SERVER_A,
                tools=(),
                scope=ToolScope.WORKSPACE,
                scope_id=SERVER_A,
            )
        with self.assertRaises(AgentPlatformError):
            catalog.replace_provenance(
                kind=ToolProvenanceKind.MCP,
                source_id=SERVER_A,
                tools=(),
                scope=ToolScope.BUILTIN,
                scope_id=SERVER_A,
            )
        with self.assertRaises(AgentPlatformError):
            catalog.replace_provenance(
                kind=ToolProvenanceKind.MCP,
                source_id=SERVER_A,
                tools="not-a-tool",
                scope=ToolScope.WORKSPACE,
                scope_id=SERVER_A,
            )
        self.assertEqual(0, catalog.generation)


if __name__ == "__main__":
    unittest.main()


class McpBridgeAdapterTest(unittest.TestCase):
    def _bridge(self, *, read_only: bool, destructive: bool = False) -> object:
        from endless_task.mcp_runtime.tool import McpToolBridge

        return McpToolBridge(
            server_name="demo",
            raw_name="echo",
            description="Echo from the demo MCP server.",
            input_schema={"type": "object"},
            read_only=read_only,
            destructive=destructive,
            timeout_seconds=10.0,
            session_provider=lambda server_name: None,
        )

    def test_read_only_bridge_maps_to_safe_agent_tool(self) -> None:
        from endless_task.mcp_runtime.tool import adapt_mcp_bridge_to_agent_tool
        from endless_task.tool_platform import ApprovalPolicy, IdempotencyPolicy, ToolEffect

        adapter = adapt_mcp_bridge_to_agent_tool(self._bridge(read_only=True))
        self.assertTrue(adapter.definition.name.startswith("mcp__"))
        self.assertEqual(ToolEffect.READ_ONLY, adapter.definition.effect)
        self.assertEqual(ApprovalPolicy.AUTO, adapter.definition.approval)
        self.assertEqual(IdempotencyPolicy.SAFE, adapter.definition.idempotency)

    def test_effectful_bridge_maps_to_approval_required_external_action(self) -> None:
        from endless_task.mcp_runtime.tool import adapt_mcp_bridge_to_agent_tool
        from endless_task.tool_platform import ApprovalPolicy, ToolEffect

        adapter = adapt_mcp_bridge_to_agent_tool(self._bridge(read_only=False, destructive=True))
        self.assertEqual(ToolEffect.EXTERNAL_ACTION, adapter.definition.effect)
        self.assertEqual(ApprovalPolicy.REQUIRED, adapter.definition.approval)
        self.assertEqual(
            frozenset({"external.action"}),
            adapter.definition.required_capabilities,
        )
