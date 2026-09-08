"""B5 用户画像的 API 与注入契约（缓存友好：版本只在内容变化时前进）。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime_v2 import MemoryScope
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2MemoryRepository,
)


class _TextProvider:
    name = "b5"

    async def stream(self, request, cancellation_token):
        del request, cancellation_token
        from endless_task.runtime import ProviderCompleted, ProviderTextDelta

        yield ProviderTextDelta(text="好的。")
        yield ProviderCompleted()


@asynccontextmanager
async def _client(database_path: Path):
    app = create_app(
        settings=AppSettings(database_path=database_path),
        provider=_TextProvider(),
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


class UserProfileApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "b5.db"
        database = Database(self.database_path)
        database.initialize()
        self.conversation_id = (
            SqliteChatRepository(database).create_conversation().id
        )
        self.memories = SqliteRuntimeV2MemoryRepository(database)
        self._add_memory("喜欢简洁回答")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_memory(self, content: str) -> None:
        self.memories.create_memory(
            scope=MemoryScope.USER_GLOBAL,
            kind="preference",
            content=content,
            conversation_id=self.conversation_id,
        )

    async def test_refresh_builds_profile_and_repeat_is_stable(self) -> None:
        async with _client(self.database_path) as client:
            first = await client.post("/user-profile/refresh", params={"force": True})
            self.assertEqual(200, first.status_code)
            payload = first.json()
            self.assertEqual(1, payload["version"])
            self.assertTrue(payload["versionChanged"])
            self.assertIn("喜欢简洁回答", payload["content"])

            # 内容未变 → 不刷新、不升版本（前缀缓存保持不变）。
            second = await client.post("/user-profile/refresh", params={"force": True})
            body = second.json()
            self.assertFalse(body["refreshed"])
            self.assertFalse(body["versionChanged"])
            self.assertEqual(1, body["version"])
            self.assertEqual(payload["signature"], body["signature"])

            fetched = await client.get("/user-profile")
            self.assertEqual(payload["content"], fetched.json()["content"])

    async def test_manual_profile_is_kept_and_editable(self) -> None:
        async with _client(self.database_path) as client:
            written = await client.put(
                "/user-profile",
                json={"content": "- 我是资深工程师\n- 直接给结论"},
            )
            self.assertEqual(200, written.status_code)
            body = written.json()
            self.assertTrue(body["manual"])
            self.assertIn("我是资深工程师", body["content"])

            # 新增记忆后自动重建不会覆盖手写画像。
            self._add_memory("后来新增的记忆")
            # 默认刷新尊重手写画像（force=true 才是显式覆盖）。
            kept = await client.post("/user-profile/refresh")
            self.assertFalse(kept.json()["refreshed"])
            self.assertNotIn("后来新增的记忆", kept.json()["content"])

            # 显式 force 会重建（手写被替换）。
            rebuilt = await client.post(
                "/user-profile/refresh", params={"force": True}
            )
            self.assertEqual(200, rebuilt.status_code)
            self.assertTrue(rebuilt.json()["refreshed"])
            self.assertIn("后来新增的记忆", rebuilt.json()["content"])

    async def test_empty_profile_is_empty_block(self) -> None:
        empty_db = Path(self._tmp.name) / "empty.db"
        async with _client(empty_db) as client:
            response = await client.get("/user-profile")
            self.assertEqual(200, response.status_code)
            self.assertEqual("", response.json()["content"])
            self.assertEqual(0, response.json()["version"])


if __name__ == "__main__":
    unittest.main()
