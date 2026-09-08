"""A5 撤销/回滚的 API 契约：真实工具写入 → 撤销 → 文件回退（幂等）。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from tests.fixtures.v2_client import (
    run_snapshot,
    send_message,
    wait_for_run_terminal,
)
from tests.test_workspace_runtime_e2e import ScriptedToolProvider


class WorkspaceUndoApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._clients: list[tuple[httpx.AsyncClient, object]] = []
        self._count = 0

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._tmp.cleanup()

    async def _client(self, provider):
        self._count += 1
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / f"undo-{self._count}.db",
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
        return client, app, str(Path(self._tmp.name) / "ws")

    async def _bound_conversation(self, client, root: str) -> str:
        created = await client.post(
            "/workspaces", json={"name": "撤销区", "rootPath": root}
        )
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    async def _run_tool(self, client, app, conversation_id: str) -> None:
        handle = await send_message(
            client, conversation_id, "开始", idempotency_key=f"k-{self._count}"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])

    async def test_write_then_undo_restores_previous_content(self) -> None:
        root = str(Path(self._tmp.name) / "ws")
        Path(root).mkdir(parents=True, exist_ok=True)
        target = Path(root) / "notes.md"
        target.write_text("旧内容", encoding="utf-8")
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="write_workspace_file",
                arguments={"path": "notes.md", "content": "agent 新内容"},
                follow_up="已写入。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        await self._run_tool(client, app, conversation_id)
        self.assertEqual("agent 新内容", target.read_text(encoding="utf-8"))

        listing = await client.get(
            f"/conversations/{conversation_id}/undo-journal"
        )
        self.assertEqual(200, listing.status_code)
        items = listing.json()["items"]
        self.assertEqual(1, len(items))
        entry = items[0]
        self.assertEqual("file_write", entry["kind"])
        self.assertEqual("notes.md", entry["target"])
        self.assertTrue(entry["undoable"])
        self.assertIn("notes.md", entry["description"])

        undone = await client.post(f"/undo-journal/{entry['id']}/undo")
        self.assertEqual(200, undone.status_code)
        body = undone.json()
        self.assertTrue(body["performed"])
        self.assertFalse(body["alreadyUndone"])
        self.assertFalse(body["entry"]["undoable"])
        self.assertEqual("旧内容", target.read_text(encoding="utf-8"))

        # 幂等：再次撤销不再改文件。
        again = await client.post(f"/undo-journal/{entry['id']}/undo")
        self.assertEqual(200, again.status_code)
        self.assertFalse(again.json()["performed"])
        self.assertTrue(again.json()["alreadyUndone"])
        self.assertEqual("旧内容", target.read_text(encoding="utf-8"))

    async def test_new_file_undo_removes_it(self) -> None:
        root = str(Path(self._tmp.name) / "ws-new")
        Path(root).mkdir(parents=True, exist_ok=True)
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="write_workspace_file",
                arguments={"path": "fresh.md", "content": "新文件"},
                follow_up="已写入。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        await self._run_tool(client, app, conversation_id)
        created = Path(root) / "fresh.md"
        self.assertTrue(created.exists())

        listing = await client.get(
            f"/conversations/{conversation_id}/undo-journal"
        )
        entry_id = listing.json()["items"][0]["id"]
        undone = await client.post(f"/undo-journal/{entry_id}/undo")
        self.assertTrue(undone.json()["performed"])
        self.assertFalse(created.exists())

    async def test_delete_then_undo_restores_file(self) -> None:
        root = str(Path(self._tmp.name) / "ws-del")
        Path(root).mkdir(parents=True, exist_ok=True)
        target = Path(root) / "doomed.txt"
        target.write_text("被删前的内容", encoding="utf-8")
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="delete_workspace_file",
                arguments={"path": "doomed.txt"},
                follow_up="已删除。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "开始", idempotency_key="k-del"
        )
        approval = None
        for _ in range(200):
            snapshot = await run_snapshot(client, conversation_id)
            approvals = snapshot.get("pendingApprovals") or ()
            if approvals:
                approval = approvals[0]
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(approval)
        resolved = await client.post(
            f"/api/v2/approvals/{approval['id']}", json={"decision": "approve"}
        )
        self.assertEqual(200, resolved.status_code)
        await wait_for_run_terminal(app.state.container, handle["runId"])
        self.assertFalse(target.exists())

        listing = await client.get(
            f"/conversations/{conversation_id}/undo-journal"
        )
        items = listing.json()["items"]
        self.assertEqual("file_delete", items[0]["kind"])
        undone = await client.post(f"/undo-journal/{items[0]['id']}/undo")
        self.assertTrue(undone.json()["performed"])
        self.assertEqual("被删前的内容", target.read_text(encoding="utf-8"))

    async def test_undo_unknown_entry_returns_404(self) -> None:
        client, _, _ = await self._client(
            ScriptedToolProvider(
                tool_name="write_workspace_file",
                arguments={"path": "x.md", "content": "x"},
                follow_up="ok",
            )
        )
        response = await client.post("/undo-journal/undo_missing/undo")
        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
