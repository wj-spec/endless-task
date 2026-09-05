"""P2-2a：skill 包 registry 只读查看端点。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app


class SkillPackagesApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple] = []

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self, skill_packages_enabled: bool):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "skills.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
                skill_packages_enabled=skill_packages_enabled,
            )
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self._clients.append((client, lifespan, app))
        return client

    async def test_legacy_mode_returns_disabled(self):
        client = await self._client(skill_packages_enabled=False)
        response = await client.get("/api/v2/skills/packages")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["mode"], "legacy")
        self.assertEqual(payload["packages"], [])

    async def test_packages_mode_empty_roots_no_crash(self):
        client = await self._client(skill_packages_enabled=True)
        response = await client.get("/api/v2/skills/packages")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["mode"], "packages")
        self.assertEqual(payload["packages"], [])
        self.assertIn("conflicts", payload)

    async def test_general_workspace_rejected(self):
        client = await self._client(skill_packages_enabled=True)
        response = await client.get(
            "/api/v2/skills/packages", params={"workspaceId": "general"}
        )
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()


class CapabilitiesFlagsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "caps.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
            )
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        return client, app

    async def test_capabilities_expose_effective_flags(self):
        client, _app = await self._client()
        response = await client.get("/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        # 8 项配置面：toolPlatformV2/contextEngineV2/delegation + 5 项新增
        for key in [
            "toolPlatformV2",
            "contextEngineV2",
            "delegation",
            "skillPackages",
            "providerRetry",
            "runtimeTrace",
            "otlpExport",
            "executionBackend",
        ]:
            self.assertIn(key, payload, key)
        self.assertFalse(payload["skillPackages"]["enabled"])
        self.assertEqual(payload["runtimeTrace"]["mode"], "all")
        self.assertEqual(payload["executionBackend"]["mode"], "local")
        self.assertFalse(payload["stopPolicy"]["enabled"])
