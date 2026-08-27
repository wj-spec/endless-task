"""R5.8 混合检索：字面 + 语义加权融合、可见性与降级路径。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional

from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.storage import Database, SqliteKnowledgeRepository


class FakeSemanticSearcher:
    """测试用评分器：预设每个 ref 的余弦分；可注入失败。"""

    def __init__(self, scores: Optional[Dict[str, float]] = None) -> None:
        self.scores = scores or {}
        self.queries: list = []
        self.embed_fails = False
        self.score_fails = False

    def embed_query(self, query: str):
        self.queries.append(query)
        return None if self.embed_fails else [1.0, 0.0]

    def score_refs(self, scope, query_vector, ref_ids):  # noqa: ARG002
        if self.score_fails:
            raise RuntimeError("semantic backend down")
        return {ref: self.scores[ref] for ref in ref_ids if ref in self.scores}


class HybridSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "hybrid.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _repository(self, searcher=None, literal=0.4, semantic=0.6):
        repository = SqliteKnowledgeRepository(
            self.database,
            hybrid_literal_weight=literal,
            hybrid_semantic_weight=semantic,
        )
        if searcher is not None:
            repository.set_semantic_searcher(searcher)
        return repository

    def _add_source(self, repository, title: str, content: str):
        return repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
        )

    def test_literal_path_unchanged_without_searcher(self) -> None:
        repository = self._repository()
        source = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        hits = repository.search("清洗奶管", [KnowledgeScope.SOURCE])
        self.assertEqual([hit.ref_id for hit in hits[KnowledgeScope.SOURCE]], [source.id])
        self.assertEqual(hits[KnowledgeScope.SOURCE][0].score, 2)

    def test_semantic_only_hit_surfaces(self) -> None:
        repository = self._repository()
        self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        source_b = self._add_source(repository, "打印机指南", "安装驱动前重启电脑")
        searcher = FakeSemanticSearcher(scores={source_b.id: 0.9})
        hybrid = self._repository(searcher)
        # 查询与两个源的字符串零重叠：纯字面应为空
        self.assertEqual(repository.search("设备保养秘诀", [KnowledgeScope.SOURCE]), {})
        hits = hybrid.search("设备保养秘诀", [KnowledgeScope.SOURCE])
        hits_list = hits[KnowledgeScope.SOURCE]
        self.assertEqual([hit.ref_id for hit in hits_list], [source_b.id])
        self.assertAlmostEqual(hits_list[0].score, 0.6 * 0.9, places=6)
        self.assertEqual(searcher.queries, ["设备保养秘诀"])

    def test_fusion_ranks_literal_plus_semantic_first(self) -> None:
        repository = self._repository()
        source_a = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        source_b = self._add_source(repository, "打印机指南", "安装驱动前重启电脑")
        searcher = FakeSemanticSearcher(
            scores={source_a.id: 0.5, source_b.id: 1.0}
        )
        hybrid = self._repository(searcher)
        hits = hybrid.search("清洗奶管", [KnowledgeScope.SOURCE])[KnowledgeScope.SOURCE]
        # A：0.4*1 + 0.6*0.5 = 0.7；B：0.6*1.0 = 0.6 → A 在前
        self.assertEqual([hit.ref_id for hit in hits], [source_a.id, source_b.id])
        self.assertAlmostEqual(hits[0].score, 0.7, places=6)
        self.assertAlmostEqual(hits[1].score, 0.6, places=6)

    def test_semantic_weight_zero_drops_semantic_only_hits(self) -> None:
        repository = self._repository()
        source_a = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        source_b = self._add_source(repository, "打印机指南", "安装驱动前重启电脑")
        searcher = FakeSemanticSearcher(scores={source_b.id: 1.0})
        hybrid = self._repository(searcher, literal=1.0, semantic=0.0)
        hits = hybrid.search("清洗奶管", [KnowledgeScope.SOURCE])[KnowledgeScope.SOURCE]
        self.assertEqual([hit.ref_id for hit in hits], [source_a.id])

    def test_expired_source_is_invisible_to_semantic(self) -> None:
        repository = self._repository()
        source = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        searcher = FakeSemanticSearcher(scores={source.id: 1.0})
        hybrid = self._repository(searcher)
        repository.expire_source(source.id)
        self.assertEqual(hybrid.search("完全无关的查询", [KnowledgeScope.SOURCE]), {})
        self.assertEqual(repository.visible_rows("source"), [])

    def test_embed_query_failure_falls_back_to_literal(self) -> None:
        repository = self._repository()
        source = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        searcher = FakeSemanticSearcher(scores={source.id: 1.0})
        searcher.embed_fails = True
        hybrid = self._repository(searcher)
        hits = hybrid.search("清洗奶管", [KnowledgeScope.SOURCE])[KnowledgeScope.SOURCE]
        self.assertEqual([hit.ref_id for hit in hits], [source.id])
        self.assertEqual(hits[0].score, 2)

    def test_score_refs_failure_falls_back_to_literal(self) -> None:
        repository = self._repository()
        source = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        searcher = FakeSemanticSearcher()
        searcher.score_fails = True
        hybrid = self._repository(searcher)
        hits = hybrid.search("清洗奶管", [KnowledgeScope.SOURCE])[KnowledgeScope.SOURCE]
        self.assertEqual([hit.ref_id for hit in hits], [source.id])
        self.assertEqual(hits[0].score, 2)

    def test_negative_cosine_is_clamped_to_zero(self) -> None:
        repository = self._repository()
        source = self._add_source(repository, "咖啡机规范", "使用咖啡机后要清洗奶管")
        searcher = FakeSemanticSearcher(scores={source.id: -0.5})
        hybrid = self._repository(searcher)
        # 无字面命中且语义分为负 → 无候选
        self.assertEqual(hybrid.search("完全无关的查询", [KnowledgeScope.SOURCE]), {})

    def test_visible_rows_include_all_active_sources(self) -> None:
        repository = self._repository()
        source_a = self._add_source(repository, "甲", "内容甲")
        source_b = self._add_source(repository, "乙", "内容乙")
        rows = repository.visible_rows(KnowledgeScope.SOURCE)
        self.assertEqual({row["ref_id"] for row in rows}, {source_a.id, source_b.id})
        self.assertEqual(repository.visible_rows("bogus"), [])


if __name__ == "__main__":
    unittest.main()
