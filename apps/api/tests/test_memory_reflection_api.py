"""B4 反思的 API 契约：重复失败 → 洞见提案 → 确认后成为重要记忆。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime.provider import ProviderStreamEvent
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


class _RepeatedFailureProvider:
    """连续两次调用同一个不存在的文件（同一错误码），然后收尾。"""

    name = "reflection"

    def __init__(self, tool_name: str = "read_workspace_file") -> None:
        self._tool_name = tool_name
        self.requests = 0

    async def stream(
        self, request: ProviderRequest, cancellation_token
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        cancellation_token.raise_if_cancelled()
        self.requests += 1
        if self.requests <= 2:
            yield ProviderToolCall(
                id=f"call_{self.requests}",
                name=self._tool_name,
                arguments={"path": "missing.txt"},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("试过了，换不了路径。")
        yield ProviderCompleted(finish_reason="stop")


class ReflectionApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._clients: list[tuple[httpx.AsyncClient, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._tmp.cleanup()

    async def _client(self, provider):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / "b4.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._clients.append((client, lifespan))
        return client, app

    async def _bound_conversation(self, client, root: str) -> str:
        created = await client.post(
            "/workspaces", json={"name": "反思区", "rootPath": root}
        )
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    async def test_repeated_failure_becomes_confirmable_insight(self) -> None:
        root = str(Path(self._tmp.name) / "ws")
        Path(root).mkdir(parents=True, exist_ok=True)
        client, app = await self._client(_RepeatedFailureProvider())
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "读一下 missing.txt", idempotency_key="k-r"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])

        listed = await client.get(f"/conversations/{conversation_id}/reflections")
        self.assertEqual(200, listed.status_code)
        items = listed.json()["items"]
        self.assertEqual(1, len(items))
        reflection = items[0]
        self.assertEqual("tool_failure", reflection["trigger"])
        self.assertEqual("pending", reflection["status"])
        self.assertIn("read_workspace_file", reflection["insight"])
        self.assertIn("不要原样重试", reflection["insight"])
        self.assertTrue(reflection["sources"])

        resolved = await client.post(
            f"/memory-proposals/{reflection['proposalId']}/resolve",
            json={"decision": "accept"},
        )
        self.assertEqual(200, resolved.status_code)
        insight_id = resolved.json()["memory"]["id"]

        memories = await client.get("/memories")
        insight = next(
            item for item in memories.json()["items"] if item["id"] == insight_id
        )
        # 洞见按"重要记忆"对待，B3 不会自动遗忘它。
        self.assertGreaterEqual(insight["importance"], 0.8)

        after = await client.get(f"/conversations/{conversation_id}/reflections")
        self.assertEqual("accepted", after.json()["items"][0]["status"])
        self.assertEqual(
            insight_id, after.json()["items"][0]["insightMemoryId"]
        )

    async def test_rejected_reflection_is_not_reproposed(self) -> None:
        root = str(Path(self._tmp.name) / "ws2")
        Path(root).mkdir(parents=True, exist_ok=True)
        client, app = await self._client(_RepeatedFailureProvider())
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "再读一次", idempotency_key="k-r2"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])
        reflection = (
            await client.get(f"/conversations/{conversation_id}/reflections")
        ).json()["items"][0]

        rejected = await client.post(
            f"/memory-proposals/{reflection['proposalId']}/resolve",
            json={"decision": "reject"},
        )
        self.assertEqual(200, rejected.status_code)
        after = await client.get(f"/conversations/{conversation_id}/reflections")
        self.assertEqual("rejected", after.json()["items"][0]["status"])
        # 反思服务不会为同一教训再提一次。
        app.state.container.memory_reflection_service.reflect_run(handle["runId"])
        self.assertEqual(
            1,
            len(
                (
                    await client.get(
                        f"/conversations/{conversation_id}/reflections"
                    )
                ).json()["items"]
            ),
        )

    async def test_clean_run_has_no_reflections(self) -> None:
        root = str(Path(self._tmp.name) / "ws3")
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / "ok.txt").write_text("内容", encoding="utf-8")

        class _CleanProvider:
            name = "clean"

            def __init__(self) -> None:
                self.requests = 0

            async def stream(self, request, cancellation_token):
                del request
                cancellation_token.raise_if_cancelled()
                self.requests += 1
                if self.requests == 1:
                    yield ProviderToolCall(
                        id="call_1",
                        name="read_workspace_file",
                        arguments={"path": "ok.txt"},
                    )
                    yield ProviderCompleted(finish_reason="tool_calls")
                    return
                yield ProviderTextDelta("读完了。")
                yield ProviderCompleted(finish_reason="stop")

        client, app = await self._client(_CleanProvider())
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "读一下", idempotency_key="k-r3"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])
        listed = await client.get(f"/conversations/{conversation_id}/reflections")
        self.assertEqual([], listed.json()["items"])


if __name__ == "__main__":
    unittest.main()
