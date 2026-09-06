"""S-P1-3b：run span 只读查看端点。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app
from endless_task.runtime import FakeProvider
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


class RuntimeV2SpansApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple] = []

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "spans.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
            ),
            provider=FakeProvider(chunks=("spans 回复",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self._clients.append((client, lifespan, app))
        return client, app

    async def test_spans_for_completed_run(self):
        client, app = await self._client()
        container = app.state.container
        conversation = await create_bound_conversation(client)
        handle = await send_message(
            client, conversation["id"], "跑一个带 span 的回合", idempotency_key="req-spans"
        )
        await wait_for_run_terminal(container, handle["runId"])

        response = await client.get(f"/api/v2/runs/{handle['runId']}/spans")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertGreaterEqual(len(payload["spans"]), 1)
        first = payload["spans"][0]
        for key in ("spanId", "kind", "name", "status", "durationMs"):
            self.assertIn(key, first, key)

    async def test_unknown_run_404(self):
        client, _app = await self._client()
        response = await client.get("/api/v2/runs/does_not_exist/spans")
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
