"""P2-1a：trajectory bundle 只读查看端点。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app


class TrajectoryViewApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple] = []

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        db_dir = Path(self._temporary_directory.name)
        export_root = db_dir / "v2_trajectory_exports"
        bundle = export_root / "run_alpha"
        bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "provider": "fake",
                    "model": "e2e",
                    "redactionPolicyRevision": "r1",
                }
            ),
            encoding="utf-8",
        )
        (bundle / "events.jsonl").write_text('{"seq":1}\n{"seq":2}\n', encoding="utf-8")
        # 非 bundle 文件不应被列出/读取
        (bundle / "secret.txt").write_text("nope", encoding="utf-8")

        app = create_app(
            settings=AppSettings(
                database_path=db_dir / "view.db",
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
        self._clients.append((client, lifespan, app))
        return client

    async def test_list_and_meta(self):
        client = await self._client()
        listed = await client.get("/api/v2/trajectory")
        self.assertEqual(listed.status_code, 200, listed.text)
        items = listed.json()["items"]
        self.assertEqual([item["runId"] for item in items], ["run_alpha"])
        self.assertEqual(items[0]["files"][0]["name"], "manifest.json")

        meta = await client.get("/api/v2/trajectory/run_alpha")
        self.assertEqual(meta.status_code, 200, meta.text)
        payload = meta.json()
        self.assertIn("events.jsonl", payload["fileNames"])
        self.assertNotIn("secret.txt", payload["fileNames"])
        self.assertEqual(payload["manifest"]["provider"], "fake")

    async def test_file_read_and_guards(self):
        client = await self._client()
        content = await client.get("/api/v2/trajectory/run_alpha/files/events.jsonl")
        self.assertEqual(content.status_code, 200, content.text)
        self.assertIn("seq", content.json()["content"])

        forbidden = await client.get("/api/v2/trajectory/run_alpha/files/secret.txt")
        self.assertEqual(forbidden.status_code, 400, forbidden.text)

        missing = await client.get("/api/v2/trajectory/nope/files/events.jsonl")
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
