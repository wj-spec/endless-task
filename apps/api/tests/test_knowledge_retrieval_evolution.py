"""R5.7 检索体验增强：归一化、同义词、作用域权重与两层优先级配额。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ConversationKind,
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.storage import (
    Database,
    SqliteKnowledgeRepository,
)
from endless_task.storage.sqlite_knowledge_repository import (
    CURATED_SCOPES,
    DEFAULT_SCOPE_WEIGHTS,
    normalize_query,
    scope_tier,
)


class NormalizeQueryTest(unittest.TestCase):
    def test_fullwidth_punctuation_and_case(self):
        self.assertEqual(
            normalize_query("　咖啡机，清洗！NaiGuan？ "), "咖啡机 清洗 naiguan"
        )

    def test_strips_cjk_symbols(self):
        self.assertEqual(normalize_query("《设计稿》——评审。"), "设计稿 评审")

    def test_empty(self):
        self.assertEqual(normalize_query(""), "")


class SynonymExpansionTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._dir.name) / "k.db")
        self.database.initialize()

    def tearDown(self):
        self._dir.cleanup()

    def _repo(self, synonym_map=None):
        return SqliteKnowledgeRepository(self.database, synonym_map=synonym_map)

    def test_synonym_expands_query_to_hit(self):
        repo = self._repo(synonym_map={"笔记本": ["电脑"]})
        repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="设备规范",
            content="公司配发的电脑需要登记序列号。",
        )
        hits = repo.search("笔记本登记", [KnowledgeScope.SOURCE])
        self.assertIn(KnowledgeScope.SOURCE, hits)

    def test_no_synonym_map_no_hit(self):
        repo = self._repo()
        repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="设备规范",
            content="公司配发的电脑需要登记序列号。",
        )
        hits = repo.search("笔记本登记", [KnowledgeScope.SOURCE])
        self.assertEqual(hits.get(KnowledgeScope.SOURCE, []), [])


class ScopeWeightAndTierTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._dir.name) / "k.db")
        self.database.initialize()

    def tearDown(self):
        self._dir.cleanup()

    def test_curated_scopes_are_tier_zero(self):
        for scope in CURATED_SCOPES:
            self.assertEqual(scope_tier(scope), 0)
        self.assertEqual(scope_tier(KnowledgeScope.ARTIFACT), 1)
        self.assertEqual(scope_tier(KnowledgeScope.CONVERSATION), 1)

    def test_weights_scale_score(self):
        repo = SqliteKnowledgeRepository(self.database)
        repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机",
            content="咖啡机使用后必须清洗奶管。",
        )
        hits = repo.search("咖啡机清洗奶管", [KnowledgeScope.SOURCE])
        hit = hits[KnowledgeScope.SOURCE][0]
        self.assertGreater(hit.score, 0)
        # curated 默认权重为 1.0，分数等于命中片段计数。
        self.assertEqual(DEFAULT_SCOPE_WEIGHTS[KnowledgeScope.SOURCE], 1.0)

    def test_custom_weight_reorders_scopes(self):
        repo = SqliteKnowledgeRepository(
            self.database,
            scope_weights={KnowledgeScope.SOURCE: 0.0},
        )
        repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机",
            content="咖啡机使用后必须清洗奶管。",
        )
        hits = repo.search("咖啡机清洗奶管", [KnowledgeScope.SOURCE])
        self.assertEqual(hits[KnowledgeScope.SOURCE][0].score, 0.0)


class PriorityQuotaInjectionTest(unittest.TestCase):
    """注入选择：curated 层优先，层内按分数。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._dir.name) / "k.db")
        self.database.initialize()
        self.repo = SqliteKnowledgeRepository(self.database)

    def tearDown(self):
        self._dir.cleanup()

    def _seed(self):
        self.repo.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="清洗规范",
            content="咖啡机奶管清洗规范：每次使用后冲洗。",
        )

    def test_context_block_prefers_curated_and_numbers_k(self):
        from endless_task.runtime.context import P0ContextBuilder
        from endless_task.storage import SqliteChatRepository

        chat = SqliteChatRepository(self.database)
        self._seed()
        builder = P0ContextBuilder(
            chat,
            system_prompt="s",
            knowledge_repository=self.repo,
            max_knowledge_hits_in_context=3,
        )
        block = builder._knowledge_block("咖啡机奶管怎么清洗")
        self.assertIn("[K1]", block)
        self.assertIn("清洗规范", block)

    def test_zero_hit_records_event_when_repo_wired(self):
        from endless_task.runtime.context import P0ContextBuilder
        from endless_task.storage import (
            SqliteChatRepository,
            SqliteRetrievalEventRepository,
        )
        from endless_task.domain.models import RetrievalEventKind

        chat = SqliteChatRepository(self.database)
        events = SqliteRetrievalEventRepository(self.database)
        builder = P0ContextBuilder(
            chat,
            system_prompt="s",
            knowledge_repository=self.repo,
            retrieval_event_repository=events,
        )
        block = builder._knowledge_block("完全无关的查询词")
        self.assertEqual(block, "")
        recent = events.list_recent(kind=RetrievalEventKind.INJECTION)
        self.assertEqual(len(recent), 1)
        self.assertTrue(recent[0].zero_hit)


if __name__ == "__main__":
    unittest.main()
