"""R5.7 检索埋点：注入/搜索/角标点击事件、引用解析与聚合统计。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    RetrievalEventKind,
)
from endless_task.runtime import FakeProvider
from endless_task.storage import (
    Database,
    SqliteRetrievalEventRepository,
)


class RetrievalEventRepositoryTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._dir.name) / "e.db")
        self.database.initialize()
        self.repo = SqliteRetrievalEventRepository(self.database)

    def tearDown(self):
        self._dir.cleanup()

    def test_zero_hit_derived_from_counts(self):
        event = self.repo.record(
            RetrievalEventKind.SEARCH, "咖啡机", hit_counts={"source": 0}
        )
        self.assertTrue(event.zero_hit)
        event = self.repo.record(
            RetrievalEventKind.SEARCH, "咖啡机", hit_counts={"source": 2}
        )
        self.assertFalse(event.zero_hit)

    def test_latest_injection_for_turn(self):
        self.repo.record(
            RetrievalEventKind.INJECTION,
            "旧的",
            turn_id="turn_1",
            detail={"citations": []},
        )
        latest = self.repo.record(
            RetrievalEventKind.INJECTION,
            "新的",
            turn_id="turn_1",
            detail={"citations": [{"label": "K1"}]},
        )
        found = self.repo.latest_injection_for_turn("turn_1")
        self.assertEqual(found.id, latest.id)
        self.assertIsNone(self.repo.latest_injection_for_turn("turn_missing"))

    def test_summarize_zero_hit_rate(self):
        self.repo.record(
            RetrievalEventKind.INJECTION, "a", hit_counts={"source": 1}
        )
        self.repo.record(RetrievalEventKind.INJECTION, "b", hit_counts={})
        stats = self.repo.summarize()
        injection = stats["injection"]
        self.assertEqual(injection["total"], 2)
        self.assertEqual(injection["zeroHit"], 1)
        self.assertEqual(injection["zeroHitRate"], 0.5)


@asynccontextmanager
async def local_client(database_path: Path, provider=None):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            knowledge_proposals_enabled=False,
        ),
        provider=provider or FakeProvider(),
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


class RetrievalEventApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self._dir.name) / "api.db"

    def tearDown(self):
        self._dir.cleanup()

    async def test_search_records_event_and_stats(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/search", json={"query": "咖啡机清洗", "scopes": ["source"]}
            )
            self.assertEqual(response.status_code, 200)
            stats = (await client.get("/retrieval-stats")).json()["stats"]
            self.assertEqual(stats["search"]["total"], 1)
            self.assertEqual(stats["search"]["zeroHit"], 1)

    async def test_search_group_order_follows_priority(self):
        async with local_client(self.database_path) as client:
            database = Database(self.database_path)
            database.initialize()
            from endless_task.storage import SqliteKnowledgeRepository

            repo = SqliteKnowledgeRepository(database)
            repo.create_source(
                kind=KnowledgeSourceKind.NOTE,
                origin=KnowledgeSourceOrigin.USER,
                title="清洗规范",
                content="咖啡机奶管清洗规范内容。",
            )
            response = await client.post(
                "/search",
                json={
                    "query": "咖啡机奶管清洗",
                    "scopes": ["conversation", "artifact", "source", "memory"],
                },
            )
            groups = response.json()["groups"]
            self.assertTrue(groups)
            self.assertEqual(groups[0]["scope"], "source")

    async def test_citation_click_event_and_validation(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/retrieval-events",
                json={
                    "kind": "citation_click",
                    "label": "K1",
                    "scope": "source",
                    "refId": "ks_1",
                    "turnId": "turn_1",
                },
            )
            self.assertEqual(response.status_code, 201)
            stats = (await client.get("/retrieval-stats")).json()["stats"]
            self.assertEqual(stats["citation_click"]["total"], 1)

            rejected = await client.post(
                "/retrieval-events", json={"kind": "injection"}
            )
            self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
