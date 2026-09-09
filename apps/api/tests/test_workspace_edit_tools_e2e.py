"""S7 端到端：edit_workspace_file / manage_workspace_paths 走真实 v2 run。

验证点：新工具在真实 run 里被模型调用 → 文件落盘 → run checkpoint + mutation
ledger 记录到（与 write_workspace_file 同一套可回滚/可审计链路）。
"""

from __future__ import annotations

import hashlib
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
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class SwappableProvider:
    name = "swappable"

    def __init__(self) -> None:
        self._inner = None

    def set(self, provider) -> None:
        self._inner = provider

    async def stream(self, request, cancellation_token):
        if self._inner is None:
            raise AssertionError("no inner provider configured")
        async for item in self._inner.stream(request, cancellation_token):
            yield item


class ScriptedToolProvider:
    """第一轮发起一个工具调用，第二轮收尾。"""

    name = "scripted-tool"

    def __init__(self, *, name: str, arguments: dict, follow_up: str = "完成。") -> None:
        self._name = name
        self._arguments = arguments
        self._follow_up = follow_up
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_1", name=self._name, arguments=self._arguments
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta(self._follow_up)
        yield ProviderCompleted(finish_reason="stop")


class WorkspaceEditToolsE2ETest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name) / "ws"
        self._root.mkdir()

    async def asyncTearDown(self) -> None:
        for client, lifespan in getattr(self, "_apps", []):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._tmp.cleanup()

    async def _app(self, provider) -> tuple[httpx.AsyncClient, object]:
        index = len(getattr(self, "_apps", []))
        swappable = SwappableProvider()
        swappable.set(provider)
        self._swappable = swappable
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / f"e2e-{index}.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=swappable,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        apps = getattr(self, "_apps", [])
        apps.append((client, lifespan))
        self._apps = apps
        return client, app

    async def _run_once(
        self, client: httpx.AsyncClient, app: object, key: str
    ) -> str:
        created = await client.post(
            "/workspaces",
            json={"name": f"E2E-{key}", "rootPath": str(self._root)},
        )
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        handle = await send_message(
            client, conversation.json()["id"], "开始", idempotency_key=key
        )
        run_id = handle["runId"]
        status = await wait_for_run_terminal(app.state.container, run_id)
        self.assertEqual(RunStatus.COMPLETED, status)
        return run_id

    def _ledger_text(self, app: object) -> str:
        path = (
            Path(app.state.container.settings.database_path).parent
            / "file-mutations.jsonl"
        )
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def test_edit_tool_runs_and_ledgers(self) -> None:
        (self._root / "note.md").write_text("旧内容\n", encoding="utf-8")
        provider = ScriptedToolProvider(
            name="edit_workspace_file",
            arguments={
                "path": "note.md",
                "old_string": "旧内容",
                "new_string": "新内容",
            },
        )
        client, app = await self._app(provider)

        run_id = await self._run_once(client, app, "k-edit")

        self.assertEqual(
            "新内容\n", (self._root / "note.md").read_text(encoding="utf-8")
        )
        ledger = self._ledger_text(app)
        self.assertIn("file_edit", ledger)
        self.assertIn(_sha256(b"\xe6\x96\xb0\xe5\x86\x85\xe5\xae\xb9\n"), ledger)
        coordinator = app.state.container.run_checkpoint_coordinator
        ref = coordinator.checkpoint_for(run_id=run_id, workspace_root=str(self._root))
        self.assertIsNotNone(ref)

    async def test_move_tool_runs_and_ledgers_both_paths(self) -> None:
        (self._root / "old.md").write_text("内容\n", encoding="utf-8")
        provider = ScriptedToolProvider(
            name="manage_workspace_paths",
            arguments={
                "operation": "move",
                "path": "old.md",
                "to_path": "new/old.md",
            },
        )
        client, app = await self._app(provider)

        await self._run_once(client, app, "k-move")

        self.assertFalse((self._root / "old.md").exists())
        self.assertEqual(
            "内容\n", (self._root / "new" / "old.md").read_text(encoding="utf-8")
        )
        ledger = self._ledger_text(app)
        self.assertIn("file_delete", ledger)
        self.assertIn("file_write", ledger)
        self.assertIn("new/old.md", ledger)


if __name__ == "__main__":
    unittest.main()
