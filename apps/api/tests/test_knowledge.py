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
        self._clients: list[tuple[httpx.AsyncClient, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(self, provider=None) -> httpx.AsyncClient:
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"p5-{suffix}.db",
                runtime="v1",
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
        self._clients.append((client, lifespan))
        return client

    @staticmethod
    async def _wait_for_terminal(client: httpx.AsyncClient, turn_id: str) -> None:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            if response.json()["turnStatus"] in {"completed", "failed", "cancelled"}:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not reach a terminal state")

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
        client = await self._client()
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
        client = await self._client()
        response = await client.post(
            "/knowledge-sources",
            json={"kind": "file", "title": "规范文件", "content": "内容"},
        )
        self.assertEqual(response.status_code, 400)

    async def test_unknown_scope_is_rejected(self):
        client = await self._client()
        response = await client.post(
            "/search", json={"query": "任意", "scopes": ["bogus"]}
        )
        self.assertEqual(response.status_code, 400)

    # ---------- 中文检索与回退 ----------

    async def test_chinese_trigram_search_matches(self):
        client = await self._client()
        await self._create_note(client, "季度目标", "第三季度要完成检索质量评估。")
        groups = await self._search(client, "检索质量")
        scopes = [group["scope"] for group in groups]
        self.assertIn("source", scopes)

    async def test_short_query_falls_back_to_like(self):
        client = await self._client()
        await self._create_note(client, "缩写备注", "OKR 评审每两周一次。")
        groups = await self._search(client, "OK")
        self.assertTrue(any(group["scope"] == "source" for group in groups))

    # ---------- 内建集合联合索引 ----------

    async def test_completed_turn_in_normal_conversation_is_searchable(self):
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        turn = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "req-1"},
            json={"content": "帮我整理量子报告的重点"},
        )
        turn_id = turn.json()["turnId"]
        await self._wait_for_terminal(client, turn_id)

        groups = await self._search(client, "量子报告", scopes=["conversation"])
        self.assertEqual(groups[0]["scope"], "conversation")
        hit = groups[0]["hits"][0]
        self.assertEqual(hit["refId"], turn_id)
        self.assertEqual(hit["conversationId"], conversation_id)

    async def test_ephemeral_branch_is_not_searchable_until_promoted(self):
        client = await self._client()
        parent_id = (await client.post("/conversations")).json()["id"]
        turn = await client.post(
            f"/conversations/{parent_id}/turns",
            headers={"Idempotency-Key": "req-1"},
            json={"content": "讨论星云方案的细节"},
        )
        await self._wait_for_terminal(client, turn.json()["turnId"])

        branch = await client.post(f"/conversations/{parent_id}/branches", json={})
        branch_id = branch.json()["conversation"]["id"]
        branch_turn = await client.post(
            f"/conversations/{branch_id}/turns",
            headers={"Idempotency-Key": "req-2"},
            json={"content": "星云方案的临时推演内容"},
        )
        await self._wait_for_terminal(client, branch_turn.json()["turnId"])

        groups = await self._search(client, "临时推演", scopes=["conversation"])
        self.assertEqual(groups, [])

        await client.post(f"/conversations/{branch_id}/promote")
        groups = await self._search(client, "临时推演", scopes=["conversation"])
        self.assertEqual(len(groups[0]["hits"]), 1)

    async def test_archived_conversation_is_hidden_from_search(self):
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        turn = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "req-1"},
            json={"content": "归档前讨论极光计划"},
        )
        await self._wait_for_terminal(client, turn.json()["turnId"])

        groups = await self._search(client, "极光计划", scopes=["conversation"])
        self.assertEqual(len(groups[0]["hits"]), 1)

        await client.patch(
            f"/conversations/{conversation_id}", json={"status": "archived"}
        )
        groups = await self._search(client, "极光计划", scopes=["conversation"])
        self.assertEqual(groups, [])


    async def test_relevant_knowledge_is_injected_into_system_context(self):
        provider = FakeProvider(chunks=("好的，已按规范回答。[K1]",))
        client = await self._client(provider)
        await self._create_note(
            client,
            "咖啡机使用规范",
            "使用咖啡机后必须清洗奶管，否则奶路会堵塞。",
        )
        create = await client.post("/conversations")
        conversation_id = create.json()["id"]
        turn = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "req-1"},
            json={"content": "咖啡机用完之后要做什么维护？"},
        )
        await self._wait_for_terminal(client, turn.json()["turnId"])

        self.assertTrue(provider.requests)
        system_content = provider.requests[-1].messages[0].content
        self.assertIn("相关知识", system_content)
        self.assertIn("清洗奶管", system_content)
        self.assertIn("[K1]", system_content)

    async def test_short_message_skips_knowledge_injection(self):
        provider = FakeProvider(chunks=("收到。",))
        client = await self._client(provider)
        await self._create_note(client, "随便一条笔记", "这里写着银河计划的口令。")
        create = await client.post("/conversations")
        conversation_id = create.json()["id"]
        turn = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "req-1"},
            json={"content": "你好"},
        )
        await self._wait_for_terminal(client, turn.json()["turnId"])
        system_content = provider.requests[-1].messages[0].content
        self.assertNotIn("相关知识", system_content)


if __name__ == "__main__":
    unittest.main()
