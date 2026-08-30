from __future__ import annotations

import tempfile
import unittest
import asyncio
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider


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
                    runtime="v1",
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
                    runtime="v1",
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
                    conversation = (await client.post("/conversations")).json()
                    created = await client.post(
                        f"/conversations/{conversation['id']}/turns",
                        headers={"Idempotency-Key": "broken-skill"},
                        json={"content": "你好"},
                    )
                    turn_id = created.json()["turnId"]
                    for _ in range(200):
                        detail = (await client.get(f"/turns/{turn_id}")).json()
                        if detail["turnStatus"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual("completed", detail["turnStatus"])
                    self.assertNotIn(
                        "broken",
                        detail["content"],
                    )
            finally:
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
