"""P5 个人知识与信息源验收：知识源生命周期与联合检索。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api.app import AppSettings, create_app
from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.runtime import FakeProvider
from endless_task.storage import (
    Database,
    SqliteKnowledgeRepository,
)
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


class KnowledgeExpiryTest(unittest.TestCase):
    """R5.6：到期扫描把过期源摘出检索结果。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "knowledge.db"
        )
        self.database.initialize()
        self.repository = SqliteKnowledgeRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_expire_due_removes_overdue_sources_from_index(self):
        source = self.repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="活动规则",
            content="促销期间每天上午十点发券。",
            expires_at="2026-08-01T00:00:00.000Z",
        )
        hits = self.repository.search("发券", [KnowledgeScope.SOURCE])
        self.assertTrue(hits)

        expired = self.repository.expire_due("2026-08-26T00:00:00.000Z")
        self.assertEqual([item.id for item in expired], [source.id])

        hits = self.repository.search("发券", [KnowledgeScope.SOURCE])
        self.assertEqual(hits, {})

        restored = self.repository.restore_source(source.id)
        self.assertEqual(restored.status.value, "active")
        hits = self.repository.search("发券", [KnowledgeScope.SOURCE])
        self.assertTrue(hits)


class KnowledgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple[httpx.AsyncClient, object, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan, _app in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(self, provider=None):
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"p5-{suffix}.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                heartbeat_seconds=0.01,
            ),
            provider=provider
            or FakeProvider(chunks=("这是助手的回答，提到了量子报告。",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self._clients.append((client, lifespan, app))
        return client, app

    @staticmethod
    async def _wait_for_terminal(container, run_id: str) -> None:
        await wait_for_run_terminal(container, run_id)

    async def _create_note(self, client, title: str, content: str, **extra):
        response = await client.post(
            "/knowledge-sources",
            json={"kind": "note", "title": title, "content": content, **extra},
        )
        assert response.status_code == 201, response.text
        return response.json()["source"]

    async def _search(self, client, query: str, scopes=None):
        payload = {"query": query}
        if scopes is not None:
            payload["scopes"] = scopes
        response = await client.post("/search", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["groups"]

    # ---------- 知识源生命周期 ----------

    async def test_note_source_lifecycle_controls_search_visibility(self):
        client, app = await self._client()
        source = await self._create_note(
            client, "品牌视觉规范", "主色使用深空蓝，辅助色为月光银。"
        )
        self.assertEqual(source["origin"], "user")

        groups = await self._search(client, "深空蓝", scopes=["source"])
        self.assertEqual(groups[0]["hits"][0]["refId"], source["id"])

        response = await client.post(f"/knowledge-sources/{source['id']}/expire")
        self.assertEqual(response.json()["source"]["status"], "expired")
        groups = await self._search(client, "深空蓝", scopes=["source"])
        self.assertEqual(groups, [])

        response = await client.post(f"/knowledge-sources/{source['id']}/restore")
        self.assertEqual(response.json()["source"]["status"], "active")
        groups = await self._search(client, "深空蓝", scopes=["source"])
        self.assertEqual(len(groups[0]["hits"]), 1)

        response = await client.delete(f"/knowledge-sources/{source['id']}")
        self.assertEqual(response.status_code, 204)
        groups = await self._search(client, "深空蓝", scopes=["source"])
        self.assertEqual(groups, [])

    async def test_file_source_requires_file_name(self):
        client, app = await self._client()
        response = await client.post(
            "/knowledge-sources",
            json={"kind": "file", "title": "规范文件", "content": "内容"},
        )
        self.assertEqual(response.status_code, 400)

    async def test_unknown_scope_is_rejected(self):
        client, app = await self._client()
        response = await client.post(
            "/search", json={"query": "任意", "scopes": ["bogus"]}
        )
        self.assertEqual(response.status_code, 400)

    # ---------- 中文检索与回退 ----------

    async def test_chinese_trigram_search_matches(self):
        client, app = await self._client()
        await self._create_note(client, "季度目标", "第三季度要完成检索质量评估。")
        groups = await self._search(client, "检索质量")
        scopes = [group["scope"] for group in groups]
        self.assertIn("source", scopes)

    async def test_short_query_falls_back_to_like(self):
        client, app = await self._client()
        await self._create_note(client, "缩写备注", "OKR 评审每两周一次。")
        groups = await self._search(client, "OK")
        self.assertTrue(any(group["scope"] == "source" for group in groups))

    # ---------- 知识注入 ----------

    async def test_relevant_knowledge_is_injected_into_system_context(self):
        provider = FakeProvider(chunks=("好的，已按规范回答。[K1]",))
        client, app = await self._client(provider)
        container = app.state.container
        await self._create_note(
            client,
            "咖啡机使用规范",
            "使用咖啡机后必须清洗奶管，否则奶路会堵塞。",
        )
        create = await create_bound_conversation(client)
        conversation_id = create["id"]
        handle = await send_message(
            client,
            conversation_id,
            "咖啡机用完之后要做什么维护？",
            idempotency_key="req-1",
        )
        await self._wait_for_terminal(container, handle["runId"])

        self.assertTrue(provider.requests)
        system_content = "".join(
            (message.content or "") for message in provider.requests[-1].messages
        )
        self.assertIn("相关知识", system_content)
        self.assertIn("清洗奶管", system_content)
        self.assertIn("[K1]", system_content)

    async def test_short_message_skips_knowledge_injection(self):
        provider = FakeProvider(chunks=("收到。",))
        client, app = await self._client(provider)
        container = app.state.container
        await self._create_note(client, "随便一条笔记", "这里写着银河计划的口令。")
        create = await create_bound_conversation(client)
        conversation_id = create["id"]
        handle = await send_message(
            client,
            conversation_id,
            "你好",
            idempotency_key="req-1",
        )
        await self._wait_for_terminal(container, handle["runId"])
        system_content = "".join(
            (message.content or "") for message in provider.requests[-1].messages
        )
        self.assertNotIn("相关知识", system_content)


    # ---------- P0-4b：conversation scope 以 v2 条目为事实源 ----------

    async def test_conversation_scope_searches_v2_transcript_entries(self):
        client, app = await self._client()
        container = app.state.container
        marker = f"v2convsearch{hex(id(self))[2:]}"
        create = await create_bound_conversation(client)
        conversation_id = create["id"]
        handle = await send_message(
            client,
            conversation_id,
            f"这条消息包含唯一标记 {marker}",
            idempotency_key="req-conv-search",
        )
        await self._wait_for_terminal(container, handle["runId"])

        response = await client.post(
            "/search",
            json={"query": marker, "scopes": ["conversation"], "limit": 5},
        )
        self.assertEqual(response.status_code, 200, response.text)
        groups = response.json()["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["scope"], "conversation")
        hits = groups[0]["hits"]
        self.assertTrue(hits, "v2 会话内容应可被 conversation scope 检索到")
        self.assertEqual(hits[0]["conversationId"], conversation_id)
        self.assertIn(marker, hits[0]["snippet"])

if __name__ == "__main__":
    unittest.main()