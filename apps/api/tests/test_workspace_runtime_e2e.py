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
from tests.fixtures.v2_client import (
    run_snapshot,
    send_message,
    wait_for_run_terminal,
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
        self._clients: list[tuple[httpx.AsyncClient, object]] = []
        self._client_count = 0

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(
        self, provider
    ) -> tuple[httpx.AsyncClient, object, str]:
        self._client_count += 1
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name)
                / f"e2e-{self._client_count}.db",
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
        return client, app, str(Path(self._temporary_directory.name) / "ws")

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
        client: httpx.AsyncClient, conversation_id: str
    ) -> dict[str, object]:
        for _ in range(200):
            snapshot = await run_snapshot(client, conversation_id)
            approvals = snapshot.get("pendingApprovals") or ()
            if approvals:
                return approvals[0]
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not request approval")

    async def test_delete_always_confirms_even_under_trust_all(self) -> None:
        client, app, root = await self._client(
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
        handle = await send_message(
            client, conversation_id, "开始", idempotency_key="k-delete"
        )
        approval = await self._wait_for_approval(client, conversation_id)
        self.assertIn("delete_workspace_file", approval["summary"])
        resolved = await client.post(
            f"/api/v2/approvals/{approval['id']}", json={"decision": "approve"}
        )
        self.assertEqual(200, resolved.status_code)
        await wait_for_run_terminal(app.state.container, handle["runId"])

    async def test_shell_commands_request_approval_and_resolve(self) -> None:
        # v2 对 run_shell（REQUIRED）统一请求确认；危险与安全命令都先确认再执行。
        client, app, root = await self._client(
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
        handle = await send_message(client, conversation_id, "开始", idempotency_key="k-rm")
        approval = await self._wait_for_approval(client, conversation_id)
        self.assertIn("run_shell", approval["summary"])
        resolved = await client.post(
            f"/api/v2/approvals/{approval['id']}", json={"decision": "approve"}
        )
        self.assertEqual(200, resolved.status_code)
        await wait_for_run_terminal(app.state.container, handle["runId"])

        plain_client, plain_app, plain_root = await self._client(
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
        plain_handle = await send_message(
            plain_client, plain_conv, "开始", idempotency_key="k-ls"
        )
        plain_approval = await self._wait_for_approval(plain_client, plain_conv)
        self.assertIn("run_shell", plain_approval["summary"])
        plain_resolved = await plain_client.post(
            f"/api/v2/approvals/{plain_approval['id']}",
            json={"decision": "approve"},
        )
        self.assertEqual(200, plain_resolved.status_code)
        await wait_for_run_terminal(plain_app.state.container, plain_handle["runId"])

    async def test_create_conversation_without_workspace_is_rejected(self) -> None:
        # 必选绑定设定：无工作区会话不再存在，创建即 409。
        provider = ScriptedToolProvider(
            tool_name="read_workspace_file",
            arguments={"path": "README.md"},
            follow_up="好的。",
        )
        client, _, _ = await self._client(provider)
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
        client, _, _ = await self._client(provider)
        created = await client.post("/workspaces", json={"name": "未绑定区"})
        workspace_id = created.json()["workspace"]["id"]
        rejected = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("workspace_not_bound", rejected.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
