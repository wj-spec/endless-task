"""17 切片③ file-preview 端点测试：工作区文件只读预览（引用溯源落点）。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from tests.fixtures.workspace_client import create_bound_workspace

WS_ROOT = Path(tempfile.gettempdir()) / f"file-preview-ws-{tempfile.gettempdir()[-6:]}"


@asynccontextmanager
async def _client(database_path: Path, workspace_root: Path):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            max_file_bytes=1_000_000,
        ),
        provider=FakeProvider(chunks=("ok",)),
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        workspace_id = await create_bound_workspace(client, name=f"预览 {uuid4hex()}")
        # 覆盖 fixture 生成的根目录内容：解析其 root path
        workspaces = await client.get("/workspaces")
        workspace = next(
            item for item in workspaces.json()["items"] if item["id"] == workspace_id
        )
        root = Path(workspace["rootPath"]).expanduser()
        yield client, app, workspace_id, root
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


def uuid4hex() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


class FilePreviewApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "preview.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_preview_returns_lines_with_numbers(self) -> None:
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            (root / "plan.md").write_text(
                "\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8"
            )
            response = await client.get(
                f"/workspaces/{ws_id}/file-preview",
                params={"path": "plan.md"},
            )
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(20, body["totalLines"])
            self.assertEqual(1, body["startLine"])
            self.assertEqual(20, body["endLine"])
            self.assertEqual(20, len(body["lines"]))
            self.assertEqual({"line": 1, "text": "line 1"}, body["lines"][0])

    async def test_preview_window_and_relative_subpath(self) -> None:
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            (root / "sub").mkdir()
            (root / "sub" / "notes.txt").write_text(
                "alpha\nbeta\ngamma\n", encoding="utf-8"
            )
            response = await client.get(
                f"/workspaces/{ws_id}/file-preview",
                params={
                    "path": "sub/notes.txt",
                    "start_line": 2,
                    "line_count": 1,
                },
            )
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(3, body["totalLines"])
            self.assertEqual(2, body["startLine"])
            self.assertEqual(2, body["endLine"])
            self.assertEqual([{"line": 2, "text": "beta"}], body["lines"])

    async def test_preview_rejects_missing_file(self) -> None:
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            response = await client.get(
                f"/workspaces/{ws_id}/file-preview",
                params={"path": "nope.md"},
            )
            self.assertEqual(404, response.status_code)
            self.assertEqual("path_not_found", response.json()["error"]["code"])

    async def test_preview_rejects_escape(self) -> None:
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            response = await client.get(
                f"/workspaces/{ws_id}/file-preview",
                params={"path": "../secret.txt"},
            )
            self.assertEqual(400, response.status_code)
            self.assertEqual("path_escape", response.json()["error"]["code"])

    async def test_preview_handles_nul_bytes_like_read_tool(self) -> None:
        # NUL 是合法 UTF-8（read_workspace_file 亦不拒绝）；行为保持一致。
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            (root / "nul.dat").write_bytes(b"a\x00b\nline2\n")
            response = await client.get(
                f"/workspaces/{ws_id}/file-preview",
                params={"path": "nul.dat"},
            )
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(2, body["totalLines"])

    async def test_preview_rejects_unknown_workspace(self) -> None:
        async with _client(self.database_path, Path(".")) as (client, app, ws_id, root):
            response = await client.get(
                "/workspaces/missing-workspace/file-preview",
                params={"path": "plan.md"},
            )
            self.assertEqual(404, response.status_code)

    async def test_preview_works_when_app_reads_settings_from_environment(self) -> None:
        """回归：`create_app()` 无显式 settings 时，处理器不能引用那个 None 参数。

        真实启动路径（`endless_task.api.main:app`）就是无参 create_app，历史上这里
        引用闭包里的 `settings`（None）导致每次预览 500。
        """
        import os
        from unittest import mock

        data_dir = Path(self._temp.name) / "env-data"
        data_dir.mkdir()
        env = {
            "ENDLESS_TASK_DB_PATH": str(self.database_path),
            "ENDLESS_TASK_DATA_DIR": str(data_dir),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            app = create_app(provider=FakeProvider(chunks=("ok",)))
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            try:
                workspace_id = await create_bound_workspace(client, name=f"env {uuid4hex()}")
                workspaces = await client.get("/workspaces")
                workspace = next(
                    item
                    for item in workspaces.json()["items"]
                    if item["id"] == workspace_id
                )
                root = Path(workspace["rootPath"]).expanduser()
                (root / "plan.md").write_text("hello\n", encoding="utf-8")
                response = await client.get(
                    f"/workspaces/{workspace_id}/file-preview",
                    params={"path": "plan.md"},
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual("hello", response.json()["lines"][0]["text"])
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()



if __name__ == "__main__":
    unittest.main()
