"""P0-2b：编辑消息重跑（resend）——覆盖本轮用户文案并生成 sibling run。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app
from endless_task.runtime import FakeProvider
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


class ResendEditTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple] = []

    def tearDown(self) -> None:
        for _client, lifespan, _app in self._clients:
            pass
        self._temporary_directory.cleanup()

    async def _client(self, provider):
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"resend-{suffix}.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self._clients.append((client, lifespan, app))
        return client, app

    @staticmethod
    async def _wait_for_terminal(container, run_id: str) -> None:
        await wait_for_run_terminal(container, run_id)

    async def test_resend_overrides_user_content_for_sibling_run(self):
        marker_new = f"edited{hex(id(self))[2:]}"
        provider = FakeProvider(chunks=("初始回答。", "改写后的回答。"))
        client, app = await self._client(provider)
        container = app.state.container
        conversation = await create_bound_conversation(client)
        conversation_id = conversation["id"]
        handle = await send_message(
            client, conversation_id, "原始未改写内容", idempotency_key="req-orig"
        )
        await self._wait_for_terminal(container, handle["runId"])

        before_messages = len(provider.requests)
        response = await client.post(
            f"/api/v2/runs/{handle['runId']}/resend",
            json={"content": f"改写后的内容 {marker_new}"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertNotEqual(payload["newRunId"], handle["runId"])
        self.assertEqual(payload["laneId"], handle["laneId"])
        self.assertEqual(payload["oldRunId"], handle["runId"])
        await self._wait_for_terminal(container, payload["newRunId"])

        self.assertGreater(len(provider.requests), before_messages)
        last_request = provider.requests[-1]
        request_text = "".join(
            (message.content or "") for message in last_request.messages
        )
        # 模型看到的直接用户指令必须是改写后内容（知识注入可保留原文作 provenance）
        user_prompts = [
            (message.content or "")
            for message in last_request.messages
            if message.role == "user"
        ]
        self.assertTrue(
            any(
                marker_new in prompt and "原始未改写内容" not in prompt
                for prompt in user_prompts
            ),
            user_prompts,
        )

        # sibling group 现在有 ≥2 个 run（原 + 编辑重跑）
        variants = container.runtime_v2_repository.list_run_variants(handle["runId"])
        self.assertGreaterEqual(len(variants), 2)

    async def test_resend_rejects_blank_content(self):
        provider = FakeProvider(chunks=("初始回答。",))
        client, app = await self._client(provider)
        container = app.state.container
        conversation = await create_bound_conversation(client)
        conversation_id = conversation["id"]
        handle = await send_message(
            client, conversation_id, "原始内容", idempotency_key="req-blank"
        )
        await self._wait_for_terminal(container, handle["runId"])
        response = await client.post(
            f"/api/v2/runs/{handle['runId']}/resend", json={"content": "   "}
        )
        self.assertIn(response.status_code, (400, 409, 422))


if __name__ == "__main__":
    unittest.main()
