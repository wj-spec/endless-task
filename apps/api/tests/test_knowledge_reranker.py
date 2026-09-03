"""R5.9 可选 LLM 重排：解析与降级。"""

from __future__ import annotations

import unittest

from endless_task.domain.models import KnowledgeHit, KnowledgeScope
from endless_task.runtime import FakeProvider, KnowledgeReranker


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


if __name__ == "__main__":
    unittest.main()
