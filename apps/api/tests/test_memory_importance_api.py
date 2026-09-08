"""B3 记忆重要性 / 钉住 / 遗忘巡检的 API 契约。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind
from endless_task.storage import Database, SqliteMemoryRepository

NOW = datetime(2026, 1, 31, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class _TextProvider:
    name = "b3"

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


class MemoryImportanceApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "b3.db"
        database = Database(self.database_path)
        database.initialize()
        self.memories = SqliteMemoryRepository(database, clock=lambda: _iso(0))
        self.memory = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户偏好周五发布。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_list_exposes_importance_fields(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.get("/memories")
        self.assertEqual(200, response.status_code)
        item = response.json()["items"][0]
        self.assertEqual(0.5, item["importance"])
        self.assertEqual(0, item["accessCount"])
        self.assertIsNone(item["lastAccessedAt"])
        self.assertFalse(item["pinned"])

    async def test_patch_importance_and_pin(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.patch(
                f"/memories/{self.memory.id}",
                json={"importance": 0.9, "pinned": True},
            )
        self.assertEqual(200, response.status_code)
        memory = response.json()["memory"]
        self.assertEqual(0.9, memory["importance"])
        self.assertTrue(memory["pinned"])
        # 落库可查。
        stored = self.memories.get_memory(self.memory.id)
        self.assertEqual(0.9, stored.importance)
        self.assertTrue(stored.pinned)

    async def test_patch_without_fields_is_rejected(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.patch(
                f"/memories/{self.memory.id}", json={}
            )
        # 没有可更新字段 → 校验错误（400），不静默成功。
        self.assertEqual(400, response.status_code)

    async def test_forgetting_preview_and_run(self) -> None:
        stale = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="很久以前的事实。",
            source_conversation_id="conv_1",
            source_turn_id="turn_2",
        )
        # 直接把时间推到很久以前（重要性保持默认 0.5）。
        database = Database(self.database_path)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                (_iso(999), stale.id),
            )
        async with _client(self.database_path) as client:
            preview = await client.get("/memories/forgetting-preview")
            self.assertEqual(200, preview.status_code)
            payload = preview.json()
            self.assertTrue(payload["dryRun"])
            self.assertIn(stale.id, [item["memoryId"] for item in payload["forgotten"]])
            # 预览不动数据。
            self.assertEqual(
                "active", self.memories.get_memory(stale.id).status.value
            )

            run = await client.post("/memories/forget")
            self.assertEqual(200, run.status_code)
            self.assertFalse(run.json()["dryRun"])
            self.assertIn(
                stale.id,
                [item["memoryId"] for item in run.json()["forgotten"]],
            )
        self.assertEqual("expired", self.memories.get_memory(stale.id).status.value)

    async def test_important_memory_only_needs_review(self) -> None:
        important = self.memories.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="非常重要的偏好。",
            source_conversation_id="conv_1",
            source_turn_id="turn_3",
        )
        self.memories.set_memory_importance(important.id, 0.95)
        database = Database(self.database_path)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                (_iso(999), important.id),
            )
        async with _client(self.database_path) as client:
            run = await client.post("/memories/forget")
        payload = run.json()
        # 重要记忆绝不进入"将被遗忘"，只进待确认。
        self.assertNotIn(
            important.id, [item["memoryId"] for item in payload["forgotten"]]
        )
        self.assertIn(
            important.id, [item["memoryId"] for item in payload["needsReview"]]
        )
        self.assertEqual(
            "active", self.memories.get_memory(important.id).status.value
        )


if __name__ == "__main__":
    unittest.main()
