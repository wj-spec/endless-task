"""P2-1c：离线 eval 批次只读查看端点。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app
from endless_task.eval.storage import SqliteEvalRepository


class EvalViewApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple] = []

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "eval.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
            )
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        repository = SqliteEvalRepository(app.state.container.database)
        batch_id = repository.create_batch(
            mode="suite", filters={"suite": "core_loop"}, judge_provider="fake"
        )
        with app.state.container.database.connect() as connection:
            connection.execute(
                "UPDATE eval_batches SET status = 'complete', "
                "run_count = 3, aggregate_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {"runCount": 3, "effective": 1.0, "scores": {}},
                        ensure_ascii=False,
                    ),
                    batch_id,
                ),
            )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self._clients.append((client, lifespan, app, batch_id))
        return client, batch_id

    async def test_list_and_detail(self):
        client, batch_id = await self._client()
        listed = await client.get("/api/v2/eval/batches")
        self.assertEqual(listed.status_code, 200, listed.text)
        items = listed.json()["items"]
        self.assertEqual([item["id"] for item in items], [batch_id])
        self.assertEqual(items[0]["status"], "complete")
        self.assertEqual(items[0]["runCount"], 3)

        detail = await client.get(f"/api/v2/eval/batches/{batch_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        payload = detail.json()
        self.assertEqual(payload["mode"], "suite")
        self.assertEqual(payload["resultCount"], 0)
        self.assertEqual(payload["aggregate"]["effective"], 1.0)

    async def test_unknown_batch_404(self):
        client, _batch_id = await self._client()
        response = await client.get("/api/v2/eval/batches/does_not_exist")
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
