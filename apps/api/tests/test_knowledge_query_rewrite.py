"""R5.7 可选 LLM query 改写：成功改写、失败回退与注入接线。"""

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
)
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.runtime.knowledge_query_rewriter import KnowledgeQueryRewriter
from endless_task.storage import Database, SqliteKnowledgeRepository
from tests.fixtures.workspace_client import create_bound_conversation


class ScriptedProvider:
    """按序返回脚本回复；rewrite 调用（首条）与主回合调用依次消耗。"""

    name = "scripted"

    def __init__(self, texts):
        self.texts = list(texts)
        self.requests = []

    async def stream(self, request, cancellation_token):
        index = len(self.requests)
        self.requests.append(request)
        text = self.texts[min(index, len(self.texts) - 1)]
        if isinstance(text, Exception):
            raise text
        yield ProviderTextDelta(text=text)
        yield ProviderCompleted()


class RewriterUnitTest(unittest.IsolatedAsyncioTestCase):
    async def test_rewrite_success(self):
        provider = ScriptedProvider(["咖啡机 奶管 清洗规范"])
        rewriter = KnowledgeQueryRewriter(provider, model="m")
        result = await rewriter.rewrite("请问那个咖啡机奶管要怎么处理呀？")
        self.assertEqual(result, "咖啡机 奶管 清洗规范")

    async def test_short_query_skipped(self):
        provider = ScriptedProvider(["x"])
        rewriter = KnowledgeQueryRewriter(provider, model="m")
        self.assertIsNone(await rewriter.rewrite("你好"))
        self.assertEqual(provider.requests, [])

    async def test_provider_error_falls_back(self):
        provider = ScriptedProvider([ProviderError("down", "不可用", retryable=True)])
        rewriter = KnowledgeQueryRewriter(provider, model="m")
        self.assertIsNone(await rewriter.rewrite("咖啡机奶管要怎么清洗？"))

    async def test_empty_output_falls_back(self):
        provider = ScriptedProvider(["   "])
        rewriter = KnowledgeQueryRewriter(provider, model="m")
        self.assertIsNone(await rewriter.rewrite("咖啡机奶管要怎么清洗？"))

    async def test_oversized_output_falls_back(self):
        provider = ScriptedProvider(["长" * 300])
        rewriter = KnowledgeQueryRewriter(provider, model="m")
        self.assertIsNone(await rewriter.rewrite("咖啡机奶管要怎么清洗？"))


@asynccontextmanager
async def rewrite_client(database_path: Path, provider):
    app = create_app(
            settings=AppSettings(
                database_path=database_path,
                runtime="v1",
                memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            knowledge_query_rewrite_enabled=True,
        ),
        provider=provider,
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


class RewriteWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self._dir.name) / "api.db"

    def tearDown(self):
        self._dir.cleanup()

    def _seed_source(self):
        database = Database(self.database_path)
        database.initialize()
        repo = SqliteKnowledgeRepository(database)
        repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后必须清洗奶管，否则奶路会堵塞。",
        )

    async def test_rewrite_used_for_injection(self):
        self._seed_source()
        provider = ScriptedProvider(["咖啡机 奶管 清洗", "回答：要清洗奶管。"])
        async with rewrite_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            response = await client.post(
                f"/conversations/{conversation['id']}/turns",
                headers={"Idempotency-Key": "rw-1"},
                json={"content": "那个机器用完之后到底要弄哪里啊？"},
            )
            turn_id = response.json()["turnId"]
            import asyncio

            for _ in range(60):
                snapshot = (await client.get(f"/turns/{turn_id}")).json()
                if snapshot["turnStatus"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.2)
            self.assertEqual(snapshot["turnStatus"], "completed")
            # 首次调用是改写（krw_ 前缀），随后才是主回合。
            self.assertTrue(provider.requests[0].request_id.startswith("krw_"))
            # 注入事件记录的是改写后的 query。
            citations = (await client.get(f"/turns/{turn_id}/citations")).json()
            self.assertEqual(len(citations["items"]), 1)
            self.assertEqual(citations["items"][0]["title"], "咖啡机规范")

    async def test_rewrite_failure_still_completes_turn(self):
        self._seed_source()
        provider = ScriptedProvider(
            [ProviderError("down", "改写失败", retryable=True), "正常回答。"]
        )
        async with rewrite_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            response = await client.post(
                f"/conversations/{conversation['id']}/turns",
                headers={"Idempotency-Key": "rw-2"},
                json={"content": "咖啡机用完之后要清洗什么部件？"},
            )
            turn_id = response.json()["turnId"]
            import asyncio

            for _ in range(60):
                snapshot = (await client.get(f"/turns/{turn_id}")).json()
                if snapshot["turnStatus"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.2)
            self.assertEqual(snapshot["turnStatus"], "completed")


if __name__ == "__main__":
    unittest.main()