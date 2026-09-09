"""P0 文件面板：工作区文件树端点（单层、只读、工作区根内）。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from tests.fixtures.workspace_client import create_bound_workspace


@asynccontextmanager
async def _client(database_path: Path):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
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
        workspace_id = await create_bound_workspace(client)
        workspaces = await client.get("/workspaces")
        workspace = next(
            item
            for item in workspaces.json()["items"]
            if item["id"] == workspace_id
        )
        yield client, workspace_id, Path(workspace["rootPath"]).expanduser()
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class WorkspaceTreeApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "tree.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_root_lists_directories_first_with_relative_paths(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            (root / "src").mkdir()
            (root / "README.md").write_text("# 标题\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

            response = await client.get(f"/workspaces/{ws_id}/tree")

            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual("", body["path"])
            self.assertEqual(str(root), body["rootPath"])
            self.assertFalse(body["truncated"])
            names = [entry["name"] for entry in body["entries"]]
            self.assertEqual(["src", "README.md"], names)
            self.assertEqual(
                [
                    {"kind": "directory", "relativePath": "src"},
                    {"kind": "file", "relativePath": "README.md"},
                ],
                [
                    {"kind": entry["kind"], "relativePath": entry["relativePath"]}
                    for entry in body["entries"]
                ],
            )

    async def test_show_hidden_reveals_dot_entries(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

            response = await client.get(
                f"/workspaces/{ws_id}/tree", params={"show_hidden": "true"}
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(
                [".env"], [entry["name"] for entry in response.json()["entries"]]
            )

    async def test_lists_subdirectory_level(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")

            response = await client.get(
                f"/workspaces/{ws_id}/tree", params={"path": "src"}
            )

            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual("src", body["path"])
            self.assertEqual(
                [("main.py", "src/main.py")],
                [(entry["name"], entry["relativePath"]) for entry in body["entries"]],
            )

    async def test_rejects_escape_and_absolute_paths(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            for raw in ("../secret", str(root / "src")):
                with self.subTest(raw=raw):
                    response = await client.get(
                        f"/workspaces/{ws_id}/tree", params={"path": raw}
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual(
                        "path_escape", response.json()["error"]["code"]
                    )

    async def test_rejects_file_path(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            (root / "README.md").write_text("# 标题\n", encoding="utf-8")

            response = await client.get(
                f"/workspaces/{ws_id}/tree", params={"path": "README.md"}
            )

            self.assertEqual(400, response.status_code)
            self.assertEqual("path_is_file", response.json()["error"]["code"])

    async def test_rejects_missing_directory(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            response = await client.get(
                f"/workspaces/{ws_id}/tree", params={"path": "nope"}
            )
            self.assertEqual(404, response.status_code)
            self.assertEqual("path_not_found", response.json()["error"]["code"])

    async def test_rejects_unknown_workspace(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            response = await client.get("/workspaces/missing/tree")
            self.assertEqual(404, response.status_code)

    async def test_excludes_symlink_escaping_workspace(self) -> None:
        async with _client(self.database_path) as (client, ws_id, root):
            outside = Path(tempfile.mkdtemp(prefix="outside-"))
            try:
                (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
                (root / "link").symlink_to(outside)
                (root / "ok.txt").write_text("ok\n", encoding="utf-8")

                response = await client.get(f"/workspaces/{ws_id}/tree")

                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    ["ok.txt"],
                    [entry["name"] for entry in response.json()["entries"]],
                )
            finally:
                (root / "link").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
