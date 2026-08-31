"""R5.12 端到端：删除/危险命令恒确认（提权也不放行）、工作区工具按会话可见。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)


class ScriptedToolProvider:
    """第一轮发出给定工具调用，后续收到工具结果后收尾。"""

    name = "scripted"

    def __init__(self, *, tool_name: str, arguments: dict, follow_up: str) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._follow_up = follow_up
        self.requests = []
        self.tool_results = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        tool_messages = [
            message
            for message in request.messages
            if getattr(message, "role", None) == "tool"
        ]
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_scripted_1",
                name=self._tool_name,
                arguments=self._arguments,
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        self.tool_results = [m.content for m in tool_messages]
        yield ProviderTextDelta(self._follow_up)
        yield ProviderCompleted(finish_reason="stop")


class WorkspaceRuntimeE2ETest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._client_count = 0

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(
        self, provider
    ) -> tuple[httpx.AsyncClient, str]:
        self._client_count += 1
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name)
                / f"e2e-{self._client_count}.db",
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        return client, str(Path(self._temporary_directory.name) / "ws")

    async def _bound_conversation(
        self, client: httpx.AsyncClient, root: str
    ) -> str:
        created = await client.post(
            "/workspaces",
            json={"name": "E2E区", "rootPath": root},
        )
        self.assertEqual(201, created.status_code)
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    @staticmethod
    async def _wait_for_approval(
        client: httpx.AsyncClient, turn_id: str
    ) -> dict[str, object]:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            approval = response.json().get("pendingApproval")
            if approval:
                return approval
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not request approval")

    @staticmethod
    async def _wait_for_terminal(
        client: httpx.AsyncClient, turn_id: str
    ) -> dict[str, object]:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            if response.json()["turnStatus"] in {"completed", "failed", "cancelled"}:
                return response.json()
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not reach a terminal state")

    async def _start_turn(
        self, client: httpx.AsyncClient, conversation_id: str, key: str
    ) -> str:
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": key},
            json={"content": "开始"},
        )
        return created.json()["turnId"]

    async def test_delete_always_confirms_even_under_trust_all(self) -> None:
        client, root = await self._client(
            ScriptedToolProvider(
                tool_name="delete_workspace_file",
                arguments={"path": "tmp.txt"},
                follow_up="已按用户确认处理。",
            )
        )
        Path(root).mkdir(exist_ok=True)
        conversation_id = await self._bound_conversation(client, root)
        escalated = await client.post(
            "/settings/permissions",
            json={"mode": "trust_all", "acknowledge": True},
        )
        self.assertEqual(200, escalated.status_code)
        turn_id = await self._start_turn(client, conversation_id, "k-delete")
        approval = await self._wait_for_approval(client, turn_id)
        self.assertEqual("pending", approval["status"])
        self.assertIn("delete_workspace_file", approval["summary"])
        resolved = await client.post(
            f"/approvals/{approval['id']}", json={"decision": "approve"}
        )
        self.assertEqual(200, resolved.status_code)
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("completed", result["turnStatus"])

    async def test_dangerous_shell_confirms_but_plain_shell_auto_runs(self) -> None:
        client, root = await self._client(
            ScriptedToolProvider(
                tool_name="run_shell",
                arguments={"command": "rm -rf build"},
                follow_up="已处理。",
            )
        )
        Path(root).mkdir(exist_ok=True)
        conversation_id = await self._bound_conversation(client, root)
        escalated = await client.post(
            "/settings/permissions",
            json={"mode": "trust_all", "acknowledge": True},
        )
        self.assertEqual(200, escalated.status_code)
        turn_id = await self._start_turn(client, conversation_id, "k-rm")
        approval = await self._wait_for_approval(client, turn_id)
        self.assertIn("run_shell", approval["summary"])

        plain_client, plain_root = await self._client(
            ScriptedToolProvider(
                tool_name="run_shell",
                arguments={"command": "ls -la"},
                follow_up="已处理。",
            )
        )
        Path(plain_root).mkdir(exist_ok=True)
        plain_conv = await self._bound_conversation(plain_client, plain_root)
        await plain_client.post(
            "/settings/permissions",
            json={"mode": "trust_all", "acknowledge": True},
        )
        plain_turn = await self._start_turn(plain_client, plain_conv, "k-ls")
        plain_result = await self._wait_for_terminal(plain_client, plain_turn)
        self.assertEqual("completed", plain_result["turnStatus"])

    async def test_create_conversation_without_workspace_is_rejected(self) -> None:
        # 必选绑定设定：无工作区会话不再存在，创建即 409。
        provider = ScriptedToolProvider(
            tool_name="read_workspace_file",
            arguments={"path": "README.md"},
            follow_up="好的。",
        )
        client, _ = await self._client(provider)
        rejected = await client.post("/conversations", json={})
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("workspace_required", rejected.json()["error"]["code"])

    async def test_create_conversation_in_unbound_workspace_is_rejected(self) -> None:
        # 必选绑定设定：未绑定目录的工作区不能承载会话。
        provider = ScriptedToolProvider(
            tool_name="list_workspace_dir",
            arguments={},
            follow_up="好的。",
        )
        client, _ = await self._client(provider)
        created = await client.post("/workspaces", json={"name": "未绑定区"})
        workspace_id = created.json()["workspace"]["id"]
        rejected = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("workspace_not_bound", rejected.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
