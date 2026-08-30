"""R5.9 可选 LLM 重排：解析、降级与注入链路集成。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator

import httpx

from endless_task.api.app import AppSettings, create_app
from endless_task.domain.models import KnowledgeHit, KnowledgeScope
from endless_task.runtime import FakeProvider, KnowledgeReranker
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
)


def _hit(ref_id: str, title: str = "标题", snippet: str = "片段") -> KnowledgeHit:
    return KnowledgeHit(
        scope=KnowledgeScope.SOURCE,
        ref_id=ref_id,
        title=title,
        snippet=snippet,
        score=1.0,
    )


class KnowledgeRerankerTest(unittest.IsolatedAsyncioTestCase):
    def _reranker(self, provider=None) -> KnowledgeReranker:
        return KnowledgeReranker(
            provider or FakeProvider(chunks=("[2, 1]",)), model="fake-model"
        )

    async def test_parses_ordering(self) -> None:
        reranker = self._reranker()
        candidates = [_hit("a"), _hit("b"), _hit("c")]
        result = await reranker.rerank("查询", candidates)
        self.assertEqual(result, ["b", "a"])

    async def test_filters_invalid_and_out_of_range_indices(self) -> None:
        reranker = self._reranker(FakeProvider(chunks=("[9, 2, -1, 2]",)))
        candidates = [_hit("a"), _hit("b")]
        result = await reranker.rerank("查询", candidates)
        self.assertEqual(result, ["b"])

    async def test_keep_limit_applies(self) -> None:
        reranker = self._reranker(FakeProvider(chunks=("[1,2,3]",)))
        candidates = [_hit("a"), _hit("b"), _hit("c")]
        result = await reranker.rerank("查询", candidates)
        self.assertEqual(result, ["a", "b", "c"])
        reranker_two = KnowledgeReranker(
            FakeProvider(chunks=("[1,2,3]",)), model="m", keep=2
        )
        result = await reranker_two.rerank("查询", candidates)
        self.assertEqual(result, ["a", "b"])

    async def test_invalid_output_returns_none(self) -> None:
        reranker = self._reranker(FakeProvider(chunks=("无法排序，请提供更多信息。",)))
        self.assertIsNone(await reranker.rerank("查询", [_hit("a")]))

    async def test_empty_inputs_return_none(self) -> None:
        reranker = self._reranker()
        self.assertIsNone(await reranker.rerank("查询", []))
        self.assertIsNone(await reranker.rerank("", [_hit("a")]))

    async def test_provider_failure_returns_none(self) -> None:
        class BrokenProvider:
            name = "broken"

            async def stream(self, request, cancellation_token):  # noqa: ARG002
                raise RuntimeError("boom")
                yield  # pragma: no cover

        reranker = self._reranker(BrokenProvider())
        self.assertIsNone(await reranker.rerank("查询", [_hit("a")]))


class RerankScriptedProvider:
    """重排请求与主回答请求返回不同脚本。"""

    name = "scripted"

    def __init__(self, rerank_text: str, answer_text: str) -> None:
        self._rerank_text = rerank_text
        self._answer_text = answer_text
        self.requests: list = []

    async def stream(self, request, cancellation_token) -> AsyncIterator:
        self.requests.append(request)
        is_rerank = any(
            "相关性排序器" in (message.content or "")
            for message in request.messages
            if message.role == "system"
        )
        yield ProviderTextDelta(self._rerank_text if is_rerank else self._answer_text)
        yield ProviderCompleted()


class RerankIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """端到端：重排改变注入顺序（字面分数更高的候选被重排压后）。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_rerank_reorders_injection(self) -> None:
        provider = RerankScriptedProvider("[2]", "回答内容 [K1]")
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "rerank.db",
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                knowledge_rerank_enabled=True,
                heartbeat_seconds=0.01,
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            # 源 A：标题/内容都含“打印机驱动”，字面分数高
            await client.post(
                "/knowledge-sources",
                json={
                    "kind": "note",
                    "title": "打印机驱动安装",
                    "content": "安装打印机驱动前先重启电脑，打印机驱动要选对版本。",
                },
            )
            # 源 B：也含“重启”可入候选池，但字面分数低于 A
            source_b = (
                await client.post(
                    "/knowledge-sources",
                    json={
                        "kind": "note",
                        "title": "设备重启顺序",
                        "content": "打印机重启要先关外设。",
                    },
                )
            ).json()["source"]

            conversation_id = (await client.post("/conversations")).json()["id"]
            response = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "rerank-1"},
                json={"content": "打印机驱动安装前是不是要重启？"},
            )
            turn_id = response.json()["turnId"]
            for _ in range(200):
                data = (await client.get(f"/turns/{turn_id}")).json()
                if data["turnStatus"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(data["turnStatus"], "completed")

            citations = (
                await client.get(f"/turns/{turn_id}/citations")
            ).json()["items"]
            self.assertTrue(citations)
            # 重排只保留了第 2 个候选（源 B）：注入首位是它
            self.assertEqual(citations[0]["refId"], source_b["id"])
            # 重排请求确实发生过
            self.assertTrue(len(provider.requests) >= 2)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_rerank_disabled_keeps_original_order(self) -> None:
        provider = RerankScriptedProvider("[2]", "回答内容")
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "norank.db",
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                heartbeat_seconds=0.01,
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            conversation_id = (await client.post("/conversations")).json()["id"]
            response = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "norank-1"},
                json={"content": "你好，随便聊聊。"},
            )
            turn_id = response.json()["turnId"]
            for _ in range(200):
                data = (await client.get(f"/turns/{turn_id}")).json()
                if data["turnStatus"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(data["turnStatus"], "completed")
            # 未启用重排：只有主回答请求，没有重排请求
            self.assertEqual(len(provider.requests), 1)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
