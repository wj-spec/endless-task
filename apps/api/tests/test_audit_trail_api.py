"""A8 审计轨迹 API：一次运行的事实 → "为什么"的可审阅轨迹。"""

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


class AuditTrailApiTest(unittest.IsolatedAsyncioTestCase):
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
                database_path=Path(self._tmp.name) / f"audit-{self._count}.db",
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
        return client, app, str(Path(self._tmp.name) / f"ws-{self._count}")

    async def _bound_conversation(self, client, root: str) -> str:
        created = await client.post(
            "/workspaces", json={"name": "审计区", "rootPath": root}
        )
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    async def test_read_only_run_trail_has_reasoning(self) -> None:
        root = str(Path(self._tmp.name) / "ws-a")
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / "a.txt").write_text("内容", encoding="utf-8")
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="read_workspace_file",
                arguments={"path": "a.txt"},
                follow_up="读完了。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "读一下", idempotency_key="k-a"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])

        response = await client.get(
            f"/api/v2/runs/{handle['runId']}/audit-trail"
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(handle["runId"], payload["runId"])
        kinds = [item["kind"] for item in payload["items"]]
        self.assertIn("run", kinds)
        self.assertIn("tool", kinds)
        tool_entry = next(
            item for item in payload["items"] if item["kind"] == "tool"
        )
        self.assertIn("只读", tool_entry["rationale"])
        self.assertEqual("info", tool_entry["severity"])
        # 只读操作没有反事实。
        self.assertEqual("", tool_entry["counterfactual"])

    async def test_write_run_trail_explains_side_effect(self) -> None:
        root = str(Path(self._tmp.name) / "ws-b")
        Path(root).mkdir(parents=True, exist_ok=True)
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="write_workspace_file",
                arguments={"path": "out.md", "content": "新内容"},
                follow_up="写好了。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "写一下", idempotency_key="k-b"
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])

        response = await client.get(
            f"/api/v2/runs/{handle['runId']}/audit-trail"
        )
        items = response.json()["items"]
        tool_entry = next(item for item in items if item["kind"] == "tool")
        self.assertEqual("warning", tool_entry["severity"])
        self.assertEqual("local_write", tool_entry["effect"])
        self.assertIn("改动数据", tool_entry["rationale"])
        self.assertIn("撤销", tool_entry["counterfactual"])

    async def test_denied_approval_appears_as_critical_with_counterfactual(
        self,
    ) -> None:
        root = str(Path(self._tmp.name) / "ws-c")
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / "gone.txt").write_text("要删的内容", encoding="utf-8")
        client, app, _ = await self._client(
            ScriptedToolProvider(
                tool_name="delete_workspace_file",
                arguments={"path": "gone.txt"},
                follow_up="好的。",
            )
        )
        conversation_id = await self._bound_conversation(client, root)
        handle = await send_message(
            client, conversation_id, "删掉", idempotency_key="k-c"
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
        await client.post(
            f"/api/v2/approvals/{approval['id']}", json={"decision": "deny"}
        )
        await wait_for_run_terminal(app.state.container, handle["runId"])

        response = await client.get(
            f"/api/v2/runs/{handle['runId']}/audit-trail"
        )
        items = response.json()["items"]
        approvals = [item for item in items if item["kind"] == "approval"]
        self.assertTrue(approvals)
        resolved = next(
            (item for item in approvals if item["decision"] == "deny"), None
        )
        self.assertIsNotNone(resolved)
        self.assertEqual("critical", resolved["severity"])
        self.assertIn("拒绝", resolved["rationale"])
        self.assertIn("如果你当时批准", resolved["counterfactual"])
        # 文件没有被删除。
        self.assertTrue((Path(root) / "gone.txt").exists())

    async def test_unknown_run_returns_404(self) -> None:
        client, _, _ = await self._client(
            ScriptedToolProvider(
                tool_name="read_workspace_file",
                arguments={"path": "a.txt"},
                follow_up="ok",
            )
        )
        response = await client.get("/api/v2/runs/run_missing/audit-trail")
        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
