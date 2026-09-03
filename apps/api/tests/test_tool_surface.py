from __future__ import annotations

import unittest
from typing import Any, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError
from endless_task.tool_platform import (
    CapabilityContext,
    CapabilityProfile,
    FakeTool,
    InMemoryToolCatalog,
    SearchToolsTool,
    SurfaceBudget,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolProvenance,
    ToolProvenanceKind,
    ToolScope,
    ToolSurfaceRequest,
    estimate_surface_tokens,
    estimate_tool_schema_tokens,
    plan_tool_surface,
)

FULL_PROFILE = CapabilityProfile(
    name="surface_full",
    allowed_capabilities=frozenset(
        {
            "workspace.read",
            "workspace.write",
            "network.outbound",
            "external.action",
            "session.query",
            "memory.read",
        }
    ),
)
READ_ONLY_PROFILE = CapabilityProfile(
    name="surface_read",
    allowed_capabilities=frozenset({"workspace.read", "memory.read", "session.query"}),
)


def fake_tool(
    name: str,
    *,
    capabilities: frozenset[str] = frozenset(),
    description: str | None = None,
    properties: Optional[Mapping[str, Any]] = None,
) -> FakeTool:
    return FakeTool(
        definition=ToolDefinitionV2(
            name=name,
            description=description or f"Use {name} for file work.",
            input_schema={
                "type": "object",
                "properties": dict(properties or {"path": {"type": "string"}}),
            },
            effect=ToolEffect.READ_ONLY,
            required_capabilities=capabilities,
        ),
        outcome=ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
        ),
    )


def provenance() -> ToolProvenance:
    return ToolProvenance(kind=ToolProvenanceKind.BUILTIN, source_id="core")


def context(profile: CapabilityProfile) -> CapabilityContext:
    return CapabilityContext(profile=profile)


def catalog_with(*tools) -> tuple[InMemoryToolCatalog, CapabilityContext]:
    catalog = InMemoryToolCatalog()
    for tool in tools:
        catalog.register(tool, scope=ToolScope.BUILTIN, provenance=provenance())
    return catalog, context(FULL_PROFILE)


def request(name: str, arguments: Mapping[str, Any]) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id="call_1",
        tool_name=name,
        arguments=dict(arguments),
        conversation_id="conversation_1",
        run_id="run_1",
        model_turn_id="model_turn_1",
        correlation_id="correlation_1",
        cancellation=_Token(),
        created_at="2026-09-03T00:00:00Z",
    )


class _Token:
    @property
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None

    async def wait(self) -> None:
        return None


class TokenEstimateTest(unittest.TestCase):
    def test_estimator_is_deterministic_and_monotone(self) -> None:
        small = fake_tool("alpha", description="short", properties={"a": {"type": "string"}})
        large = fake_tool(
            "beta",
            description="A much longer description text that should increase the estimate.",
            properties={"a": {"type": "string"}, "b": {"type": "integer"}},
        )
        self.assertEqual(
            estimate_tool_schema_tokens(small.definition),
            estimate_tool_schema_tokens(small.definition),
        )
        self.assertGreaterEqual(
            estimate_tool_schema_tokens(large.definition),
            estimate_tool_schema_tokens(small.definition),
        )
        self.assertGreaterEqual(estimate_tool_schema_tokens(small.definition), 1)

    def test_surface_estimate_sums_registrations(self) -> None:
        catalog, _ = catalog_with(fake_tool("alpha"), fake_tool("beta"))
        surface = catalog.surface(ToolSurfaceRequest(context=context(FULL_PROFILE)))
        expected = sum(
            estimate_tool_schema_tokens(registration.definition)
            for registration in surface.registrations
        )
        self.assertEqual(expected, estimate_surface_tokens(surface.registrations))


class SurfacePlannerTest(unittest.TestCase):
    def _surface(self, catalog: InMemoryToolCatalog):
        return catalog.surface(ToolSurfaceRequest(context=context(FULL_PROFILE)))

    def test_under_budget_injects_the_full_surface(self) -> None:
        catalog, _ = catalog_with(fake_tool("alpha"), fake_tool("beta"))
        plan = plan_tool_surface(
            self._surface(catalog),
            SurfaceBudget(
                max_estimated_tokens=10_000,
                core_tool_names=frozenset({"alpha"}),
                search_tool_name="search_tools",
            ),
        )
        self.assertTrue(plan.inject_full)
        self.assertEqual({"alpha", "beta"}, set(plan.injected_names))
        self.assertEqual((), plan.deferred)
        self.assertFalse(plan.over_budget)
        self.assertFalse(plan.search_tools_available)

    def test_over_budget_injects_core_and_search_only(self) -> None:
        tools = (
            fake_tool("core_read", description="x" * 400),
            fake_tool("extra_a", description="y" * 400),
            fake_tool("extra_b", description="z" * 400),
            fake_tool("search_tools", description="Discovery tool."),
        )
        catalog, _ = catalog_with(*tools)
        surface = self._surface(catalog)
        budget = SurfaceBudget(
            max_estimated_tokens=10,
            core_tool_names=frozenset({"core_read"}),
            search_tool_name="search_tools",
        )
        plan = plan_tool_surface(surface, budget)
        self.assertFalse(plan.inject_full)
        self.assertTrue(plan.over_budget)
        self.assertTrue(plan.search_tools_available)
        self.assertEqual(
            {"core_read", "search_tools"},
            set(plan.injected_names),
        )
        self.assertEqual({"extra_a", "extra_b"}, set(plan.deferred_names))
        self.assertEqual(
            {item.definition.name for item in surface.registrations},
            set(plan.injected_names) | set(plan.deferred_names),
        )
        self.assertEqual(estimate_surface_tokens(surface.registrations), plan.estimated_tokens)

    def test_over_budget_without_search_tools_fails_closed(self) -> None:
        catalog, _ = catalog_with(
            fake_tool("core_read", description="x" * 400),
            fake_tool("extra_a", description="y" * 400),
        )
        with self.assertRaises(AgentPlatformError):
            plan_tool_surface(
                self._surface(catalog),
                SurfaceBudget(
                    max_estimated_tokens=10,
                    core_tool_names=frozenset({"core_read"}),
                    search_tool_name="search_tools",
                ),
            )

    def test_planning_is_deterministic(self) -> None:
        tools = (
            fake_tool("core_read", description="x" * 400),
            fake_tool("extra_a", description="y" * 400),
            fake_tool("search_tools"),
        )
        catalog, _ = catalog_with(*tools)
        budget = SurfaceBudget(
            max_estimated_tokens=10,
            core_tool_names=frozenset({"core_read"}),
            search_tool_name="search_tools",
        )
        first = plan_tool_surface(self._surface(catalog), budget)
        second = plan_tool_surface(self._surface(catalog), budget)
        self.assertEqual(first.injected_names, second.injected_names)
        self.assertEqual(first.deferred_names, second.deferred_names)
        self.assertEqual(first.estimated_tokens, second.estimated_tokens)

    def test_budget_validation(self) -> None:
        with self.assertRaises(AgentPlatformError):
            SurfaceBudget(
                max_estimated_tokens=0,
                core_tool_names=frozenset(),
                search_tool_name="search_tools",
            )
        with self.assertRaises(AgentPlatformError):
            SurfaceBudget(
                max_estimated_tokens=10,
                core_tool_names="core_read",
                search_tool_name="search_tools",
            )


class SearchToolsToolTest(unittest.IsolatedAsyncioTestCase):
    def _tool(
        self,
        catalog: InMemoryToolCatalog,
        *,
        profile: CapabilityProfile = FULL_PROFILE,
        max_output_characters: int = 12_000,
    ) -> SearchToolsTool:
        return SearchToolsTool(
            catalog=catalog,
            context_provider=lambda: context(profile),
            max_output_characters=max_output_characters,
        )

    async def test_search_returns_authorized_tool_with_parameter_summary(self) -> None:
        catalog, _ = catalog_with(
            fake_tool("alpha_read", capabilities=frozenset({"workspace.read"})),
            fake_tool(
                "beta_net",
                capabilities=frozenset({"network.outbound"}),
                properties={"url": {"type": "string"}, "method": {"type": "string"}},
            ),
        )
        tool = self._tool(catalog)
        outcome = await tool.execute(request("search_tools", {"query": "alpha"}))
        self.assertEqual("completed", outcome.status.value)
        self.assertIn("alpha_read", outcome.content)
        self.assertNotIn("beta_net", outcome.content)
        rows = list(outcome.structured_content or [])
        self.assertEqual(1, len(rows))
        self.assertEqual("alpha_read", rows[0]["name"])
        self.assertEqual(["workspace.read"], list(rows[0]["capabilities"]))
        self.assertEqual({"path": "string"}, dict(rows[0]["parameters"]))

    async def test_search_never_surfaces_capability_denied_tools(self) -> None:
        catalog, _ = catalog_with(
            fake_tool("alpha_read", capabilities=frozenset({"workspace.read"})),
            fake_tool(
                "beta_net",
                capabilities=frozenset({"network.outbound"}),
                properties={"url": {"type": "string"}},
            ),
        )
        tool = self._tool(catalog, profile=READ_ONLY_PROFILE)
        outcome = await tool.execute(request("search_tools", {"query": "beta"}))
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual([], list(outcome.structured_content or []))
        self.assertIn("未找到匹配工具", outcome.content)

    async def test_invalid_query_fails_closed(self) -> None:
        catalog, _ = catalog_with(fake_tool("alpha_read"))
        tool = self._tool(catalog)
        outcome = await tool.execute(request("search_tools", {"query": ""}))
        self.assertEqual("failed", outcome.status.value)
        self.assertEqual("invalid_search_query", outcome.diagnostic.code)

    async def test_oversized_results_are_truncated_with_flag(self) -> None:
        catalog, _ = catalog_with(
            fake_tool("alpha_read", description="word " * 200),
            fake_tool("beta_net", description="text " * 200),
        )
        tool = self._tool(catalog, max_output_characters=80)
        outcome = await tool.execute(request("search_tools", {"query": "read"}))
        self.assertEqual("completed", outcome.status.value)
        self.assertTrue(outcome.is_truncated)
        self.assertLessEqual(len(outcome.content), 120)


if __name__ == "__main__":
    unittest.main()
