"""B2 记忆巩固的 API 契约：生成提案 → 确认 → 原记忆标记已并入。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind
from endless_task.storage import Database, SqliteMemoryRepository


class _TextProvider:
    name = "b2"

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


class MemoryConsolidationApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "b2.db"
        database = Database(self.database_path)
        database.initialize()
        self.memories = SqliteMemoryRepository(database)
        self.first = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户偏好周五发布版本",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.second = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户偏好周五发布新版本",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_consolidate_creates_proposal_and_accept_merges(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post("/memories/consolidate")
            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual(1, payload["createdCount"])
            candidate = payload["created"][0]
            self.assertEqual(
                {self.first.id, self.second.id}, set(candidate["memoryIds"])
            )
            proposal_id = candidate["proposalId"]

            # 重复调用不会重复出提案（签名去重）。
            again = await client.post("/memories/consolidate")
            self.assertEqual(0, again.json()["createdCount"])

            records = await client.get("/memories/consolidations")
            self.assertEqual(200, records.status_code)
            item = records.json()["items"][0]
            self.assertEqual("pending", item["status"])
            self.assertEqual(2, len(item["sources"]))

            resolved = await client.post(
                f"/memory-proposals/{proposal_id}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, resolved.status_code)
            body = resolved.json()
            self.assertEqual(
                {self.first.id, self.second.id},
                set(body["consolidatedMemoryIds"]),
            )
            insight_id = body["memory"]["id"]

        for memory_id in (self.first.id, self.second.id):
            stored = self.memories.get_memory(memory_id)
            self.assertEqual("expired", stored.status.value)
            self.assertEqual("consolidated", stored.expired_reason)
            self.assertEqual(insight_id, stored.superseded_by)
        insight = self.memories.get_memory(insight_id)
        self.assertEqual("active", insight.status.value)

        async with _client(self.database_path) as client:
            records = await client.get("/memories/consolidations")
        item = records.json()["items"][0]
        self.assertEqual("accepted", item["status"])
        self.assertEqual(insight_id, item["insightMemoryId"])

    async def test_reject_keeps_memories_and_records_decision(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post("/memories/consolidate")
            proposal_id = response.json()["created"][0]["proposalId"]
            resolved = await client.post(
                f"/memory-proposals/{proposal_id}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(200, resolved.status_code)
            records = await client.get("/memories/consolidations")
        item = records.json()["items"][0]
        self.assertEqual("rejected", item["status"])
        self.assertEqual(
            "active", self.memories.get_memory(self.first.id).status.value
        )

    async def test_no_cluster_means_no_proposal(self) -> None:
        database = Database(self.database_path)
        extra = SqliteMemoryRepository(database)
        extra.create_memory(
            kind=MemoryKind.FACT,
            content="完全无关的另一件事",
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
        )
        async with _client(self.database_path) as client:
            response = await client.post("/memories/consolidate")
        self.assertEqual(1, response.json()["createdCount"])


if __name__ == "__main__":
    unittest.main()
