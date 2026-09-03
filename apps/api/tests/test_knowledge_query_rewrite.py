"""R5.7 可选 LLM query 改写：成功改写与失败回退。"""

from __future__ import annotations

import unittest

from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.runtime.knowledge_query_rewriter import KnowledgeQueryRewriter


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


if __name__ == "__main__":
    unittest.main()
