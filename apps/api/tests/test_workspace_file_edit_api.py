"""P1 文件编辑保存：读取 + 乐观并发写入 + 撤销日志联动。"""

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
        yield client
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def _bound(client: httpx.AsyncClient):
    workspace_id = await create_bound_workspace(client)
    conversation = await client.post(
        "/conversations", json={"workspaceId": workspace_id}
    )
    conversation.raise_for_status()
    conversation_id = conversation.json()["id"]
    workspaces = await client.get("/workspaces")
    workspace = next(
        item for item in workspaces.json()["items"] if item["id"] == workspace_id
    )
    root = Path(workspace["rootPath"]).expanduser()
    return workspace_id, conversation_id, root


class WorkspaceFileEditApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "edit.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_read_returns_content_and_stable_version(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, _cid, root = await _bound(client)
            (root / "note.md").write_text("# 标题\n正文\n", encoding="utf-8")

            first = await client.get(f"/workspaces/{ws_id}/file", params={"path": "note.md"})
            second = await client.get(f"/workspaces/{ws_id}/file", params={"path": "note.md"})

            self.assertEqual(200, first.status_code)
            body = first.json()
            self.assertEqual("# 标题\n正文\n", body["content"])
            self.assertEqual(2, body["totalLines"])
            self.assertTrue(body["version"])
            self.assertEqual(body["version"], second.json()["version"])

    async def test_read_rejects_escape_directory_and_missing(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, _cid, root = await _bound(client)
            (root / "sub").mkdir()

            escape = await client.get(f"/workspaces/{ws_id}/file", params={"path": "../x"})
            self.assertEqual(400, escape.status_code)
            self.assertEqual("path_escape", escape.json()["error"]["code"])

            directory = await client.get(f"/workspaces/{ws_id}/file", params={"path": "sub"})
            self.assertEqual(400, directory.status_code)
            self.assertEqual("path_is_directory", directory.json()["error"]["code"])

            missing = await client.get(f"/workspaces/{ws_id}/file", params={"path": "nope"})
            self.assertEqual(404, missing.status_code)

    async def test_read_rejects_binary_content(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, _cid, root = await _bound(client)
            (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

            response = await client.get(
                f"/workspaces/{ws_id}/file", params={"path": "blob.bin"}
            )

            self.assertEqual(400, response.status_code)
            self.assertEqual("binary_content", response.json()["error"]["code"])

    async def test_write_with_matching_version_saves_and_records_undo(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)
            (root / "note.md").write_text("旧内容\n", encoding="utf-8")
            current = (
                await client.get(f"/workspaces/{ws_id}/file", params={"path": "note.md"})
            ).json()

            response = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "note.md", "conversation_id": cid},
                json={"content": "新内容\n", "version": current["version"]},
            )

            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertNotEqual(current["version"], body["version"])
            self.assertTrue(body["undoEntryId"])
            self.assertEqual("新内容\n", (root / "note.md").read_text(encoding="utf-8"))

            journal = await client.get(f"/conversations/{cid}/undo-journal")
            entries = journal.json()["items"]
            self.assertEqual(1, len(entries))
            self.assertEqual("file_write", entries[0]["kind"])
            self.assertEqual("note.md", entries[0]["target"])

    async def test_write_with_stale_version_conflicts_with_current_content(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)
            (root / "note.md").write_text("版本一\n", encoding="utf-8")
            first = (
                await client.get(f"/workspaces/{ws_id}/file", params={"path": "note.md"})
            ).json()
            # 外部（或 Agent）先改一次。
            (root / "note.md").write_text("版本二\n", encoding="utf-8")

            response = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "note.md", "conversation_id": cid},
                json={"content": "面板内容\n", "version": first["version"]},
            )

            self.assertEqual(409, response.status_code)
            error = response.json()["error"]
            self.assertEqual("file_version_conflict", error["code"])
            self.assertEqual("版本二\n", error["details"]["currentContent"])
            self.assertTrue(error["details"]["currentVersion"])
            # 冲突时绝不落盘。
            self.assertEqual("版本二\n", (root / "note.md").read_text(encoding="utf-8"))

    async def test_write_without_version_cannot_overwrite_existing_file(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)
            (root / "note.md").write_text("已有内容\n", encoding="utf-8")

            response = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "note.md", "conversation_id": cid},
                json={"content": "盲写\n"},
            )

            self.assertEqual(409, response.status_code)
            self.assertEqual("已有内容\n", (root / "note.md").read_text(encoding="utf-8"))

    async def test_write_creates_new_file_without_version(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)

            response = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "sub/new.md", "conversation_id": cid},
                json={"content": "新建\n"},
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual("新建\n", (root / "sub" / "new.md").read_text(encoding="utf-8"))
            journal = await client.get(f"/conversations/{cid}/undo-journal")
            self.assertEqual("file_write", journal.json()["items"][0]["kind"])

    async def test_write_undo_restores_previous_content(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)
            (root / "note.md").write_text("原内容\n", encoding="utf-8")
            current = (
                await client.get(f"/workspaces/{ws_id}/file", params={"path": "note.md"})
            ).json()

            saved = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "note.md", "conversation_id": cid},
                json={"content": "改过的内容\n", "version": current["version"]},
            )
            undo_entry_id = saved.json()["undoEntryId"]

            undone = await client.post(f"/undo-journal/{undo_entry_id}/undo")

            self.assertEqual(200, undone.status_code)
            self.assertTrue(undone.json()["performed"])
            self.assertEqual("原内容\n", (root / "note.md").read_text(encoding="utf-8"))

    async def test_write_requires_conversation_and_matching_workspace(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, _cid, root = await _bound(client)
            other_ws_id = await create_bound_workspace(client)
            other = await client.post(
                "/conversations", json={"workspaceId": other_ws_id}
            )
            other.raise_for_status()
            other_cid = other.json()["id"]

            missing = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "a.md"},
                json={"content": "x"},
            )
            self.assertEqual(400, missing.status_code)
            self.assertEqual("conversation_required", missing.json()["error"]["code"])

            mismatch = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "a.md", "conversation_id": other_cid},
                json={"content": "x"},
            )
            self.assertEqual(400, mismatch.status_code)
            self.assertEqual(
                "conversation_workspace_mismatch", mismatch.json()["error"]["code"]
            )

    async def test_write_rejects_oversize_and_directory(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)
            (root / "sub").mkdir()

            oversize = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "big.md", "conversation_id": cid},
                json={"content": "x" * 1_000_001},
            )
            self.assertEqual(400, oversize.status_code)
            self.assertEqual("write_too_large", oversize.json()["error"]["code"])

            directory = await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "sub", "conversation_id": cid},
                json={"content": "x"},
            )
            self.assertEqual(400, directory.status_code)
            self.assertEqual("path_is_directory", directory.json()["error"]["code"])

    async def test_write_records_human_audit_entry(self) -> None:
        async with _client(self.database_path) as client:
            ws_id, cid, root = await _bound(client)

            await client.put(
                f"/workspaces/{ws_id}/file",
                params={"path": "audit.md", "conversation_id": cid},
                json={"content": "审计\n"},
            )

            log = await client.get(f"/workspaces/{ws_id}/shell-log")
            entries = log.json()["items"]
            self.assertTrue(
                any(
                    item.get("operation") == "human_write"
                    and item.get("approver") == "user"
                    and item.get("detail") == "audit.md"
                    for item in entries
                ),
                entries,
            )


if __name__ == "__main__":
    unittest.main()
