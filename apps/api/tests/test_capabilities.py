from __future__ import annotations

import tempfile
import unittest
import asyncio
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from tests.fixtures.v2_client import (
    assistant_text,
    run_snapshot,
    send_message,
    wait_for_run_terminal,
)
from tests.fixtures.workspace_client import create_bound_conversation


class CapabilitiesApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_configuration_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/capabilities")
                    self.assertEqual(200, response.status_code)
                    payload = response.json()
                    self.assertEqual("ok", payload["summary"]["state"])
                    self.assertEqual([], payload["summary"]["issues"])
                    self.assertEqual("ok", payload["skills"]["state"])
                    self.assertEqual("ok", payload["mcp"]["state"])
                    self.assertEqual("ok", payload["provider"]["state"])
                    self.assertEqual("ok", payload["embedding"]["state"])
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_invalid_skill_degrades_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            skills.mkdir()
            broken = skills / "broken"
            broken.mkdir()
            (broken / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
            app = create_app(
                settings=AppSettings(
                    database_path=root / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/capabilities")
                    self.assertEqual(200, response.status_code)
                    payload = response.json()
                    self.assertEqual("degraded", payload["summary"]["state"])
                    self.assertIn("skill:diagnostics", payload["summary"]["issues"])
                    self.assertEqual("degraded", payload["skills"]["state"])
                    self.assertEqual(1, payload["skills"]["total"])
                    self.assertEqual(
                        "missing_frontmatter",
                        payload["skills"]["diagnostics"][0]["code"],
                    )
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_unconfigured_default_provider_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    created = await client.post(
                        "/providers",
                        json={"name": "Missing key", "defaultModel": "model"},
                    )
                    self.assertEqual(201, created.status_code)
                    profile = created.json()["profile"]
                    changed = await client.post(
                        "/providers/default",
                        json={"profileId": profile["id"]},
                    )
                    self.assertEqual(200, changed.status_code)
                    response = await client.get("/capabilities")
                    payload = response.json()
                    self.assertEqual("unavailable", payload["summary"]["state"])
                    self.assertIn("provider:unconfigured", payload["summary"]["issues"])
                    self.assertEqual("unavailable", payload["provider"]["state"])
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_invalid_skill_does_not_block_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "skills" / "broken"
            broken.mkdir(parents=True)
            (broken / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
            app = create_app(
                settings=AppSettings(
                    database_path=root / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    conversation = await create_bound_conversation(client)
                    container = app.state.container
                    handle = await send_message(
                        client,
                        conversation["id"],
                        "你好",
                        idempotency_key="broken-skill",
                    )
                    await wait_for_run_terminal(container, handle["runId"])
                    snapshot = await run_snapshot(client, conversation["id"])
                    content = assistant_text(snapshot)
                    self.assertNotIn(
                        "broken",
                        content,
                    )
            finally:
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()

class ToolPlatformV2CapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    async def _payload(self, *, enabled: bool) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    tool_platform_v2_enabled=enabled,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/capabilities")
                    self.assertEqual(200, response.status_code)
                    return response.json()["toolPlatformV2"]
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_flag_off_echoes_disabled_without_profile(self) -> None:
        payload = await self._payload(enabled=False)
        self.assertFalse(payload["enabled"])
        self.assertIsNone(payload["profileName"])

    async def test_flag_on_echoes_enabled_with_calibrated_profile(self) -> None:
        payload = await self._payload(enabled=True)
        self.assertTrue(payload["enabled"])
        self.assertEqual("openai_compatible_default", payload["profileName"])

    def test_strict_flag_parsing_fails_closed_on_illegal_values(self) -> None:
        from endless_task.api.app_settings import _parse_strict_flag

        self.assertTrue(_parse_strict_flag("1", name="F"))
        self.assertTrue(_parse_strict_flag("TRUE", name="F"))
        self.assertFalse(_parse_strict_flag("0", name="F"))
        self.assertFalse(_parse_strict_flag("off", name="F"))
        with self.assertRaises(ValueError):
            _parse_strict_flag("maybe", name="F")


class ContextEngineV2CapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    async def _flag(self, *, enabled: bool) -> bool:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    context_engine_v2_enabled=enabled,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/capabilities")
                    self.assertEqual(200, response.status_code)
                    return response.json()["contextEngineV2"]["enabled"]
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_default_off_and_echo(self) -> None:
        self.assertFalse(await self._flag(enabled=False))
        self.assertTrue(await self._flag(enabled=True))
