"""M4A DR-2 slice 3: composition-root delegation wiring (flag gated)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken


def _settings(directory: str, *, delegation: bool) -> AppSettings:
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        tool_platform_v2_enabled=True,
        delegation_mode=("readonly" if delegation else "0"),
    )


class DelegationCompositionTest(unittest.IsolatedAsyncioTestCase):
    async def _container_app(self, *, delegation: bool):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=_settings(directory, delegation=delegation),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    yield client, app
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_flag_off_registers_no_delegation_tools(self) -> None:
        async for client, app in self._container_app(delegation=False):
            response = await client.get("/capabilities")
            payload = response.json()
            self.assertEqual(False, payload["delegation"]["enabled"])
            self.assertIsNone(payload["delegation"]["mode"])
            container = app.state.container
            definitions = {
                item.name for item in container.tool_registry.definitions()
            }
            self.assertNotIn("spawn_agent", definitions)
            self.assertIsNone(container.delegation_handler)

    async def test_flag_on_registers_tools_and_echoes_capability(self) -> None:
        async for client, app in self._container_app(delegation=True):
            response = await client.get("/capabilities")
            payload = response.json()
            self.assertEqual(True, payload["delegation"]["enabled"])
            self.assertEqual("readonly", payload["delegation"]["mode"])
            container = app.state.container
            definitions = {
                item.name for item in container.tool_registry.definitions()
            }
            self.assertIn("spawn_agent", definitions)
            self.assertIn("query_agent", definitions)
            self.assertIn("cancel_agent", definitions)
            self.assertIsNotNone(container.delegation_handler)

    async def test_capability_provider_does_not_crash_on_v1_definitions(self) -> None:
        # Regression (real-provider E2E, 06 §27): the composition capability
        # provider read tool.definition.required_capabilities on v1 legacy
        # ToolDefinitions (no such field) and raised AttributeError on every
        # real spawn; it now adapts through LegacyToolAdapter like the v2
        # surface provider.
        async for client, app in self._container_app(delegation=True):
            container = app.state.container
            from endless_task.tool_platform import ToolExecutionRequest

            request = ToolExecutionRequest(
                tool_name="spawn_agent",
                call_id="call_1",
                arguments={"task": "x"},
                conversation_id="conv_unused",
                run_id="run_unused",
                model_turn_id="turn_1",
                correlation_id="corr_1",
                cancellation=CancellationToken(),
                created_at="2026-09-05T00:00:00Z",
            )
            granted = container.delegation_handler._capability_provider(request)
            self.assertIsInstance(granted, frozenset)
            # No crash on v1 definitions is the regression; an unbound
            # conversation correctly loses agent.delegate (fail-closed
            # delegate gate, AP-105a parity).
            self.assertNotIn("agent.delegate", granted)
            self.assertTrue(granted <= {"agent.delegate", "external.action", "session.query", "workspace.read", "workspace.write", "workspace.delete", "process.spawn", "process.signal"})


class DelegationFlagParsingTest(unittest.TestCase):
    def test_strict_mode_parse(self) -> None:
        from endless_task.api.app import _parse_delegation_mode

        self.assertEqual("0", _parse_delegation_mode("0"))
        self.assertEqual("readonly", _parse_delegation_mode("readonly"))
        self.assertEqual("readonly", _parse_delegation_mode("1"))
        with self.assertRaises(ValueError):
            _parse_delegation_mode("isolated_write")
        with self.assertRaises(ValueError):
            _parse_delegation_mode("bogus")


if __name__ == "__main__":
    unittest.main()
