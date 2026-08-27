"""R5.10 引用反馈验收：点击/忽略聚合为排序权重因子。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    RetrievalEventKind,
)
from endless_task.domain.repositories import NotFoundError
from endless_task.knowledge.feedback import CitationFeedbackProvider
from endless_task.storage import (
    Database,
    SqliteKnowledgeRepository,
    SqliteRetrievalEventRepository,
)

REPORT_CONTENT = "季度报告模板可以在内网下载，填写后提交给主管审核。"


class CitationFeedbackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "feedback.db"
        )
        self.database.initialize()
        self.knowledge = SqliteKnowledgeRepository(self.database)
        self.events = SqliteRetrievalEventRepository(self.database)
        self.now = [0.0]

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def make_provider(self, *, with_resolver: bool = True) -> CitationFeedbackProvider:
        resolver = None
        if with_resolver:
            def resolver(ref_id: str):
                try:
                    return self.knowledge.get_chunk(ref_id)
                except NotFoundError:
                    return None

        return CitationFeedbackProvider(
            self.events,
            source_resolver=resolver,
            cache_ttl_seconds=60.0,
            clock=lambda: self.now[0],
        )

    def record_injection(self, refs, *, turn_id: str = "turn_inject") -> None:
        citations = []
        for index, (ref_id, source_id) in enumerate(refs):
            entry = {
                "label": f"K{index + 1}",
                "scope": "source",
                "refId": ref_id,
            }
            if source_id is not None:
                entry["sourceId"] = source_id
            citations.append(entry)
        self.events.record(
            RetrievalEventKind.INJECTION,
            "季度报告",
            hit_counts={"source": len(refs)},
            turn_id=turn_id,
            detail={"citations": citations},
        )

    def record_click(self, ref_id: str, *, source_id: str | None = None) -> None:
        detail: dict = {"label": "K1", "scope": "source", "refId": ref_id}
        if source_id is not None:
            detail["sourceId"] = source_id
        self.events.record(
            RetrievalEventKind.CITATION_CLICK,
            "季度报告",
            turn_id="turn_inject",
            detail=detail,
        )

    def add_note(self, *, title: str, content: str = REPORT_CONTENT):
        return self.knowledge.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
        )


class FactorAggregationTest(CitationFeedbackTestCase):
    def test_no_events_factor_is_one(self) -> None:
        provider = self.make_provider()
        self.assertEqual(provider.factor("ks_anything"), 1.0)

    def test_ignored_source_is_penalized(self) -> None:
        source = self.add_note(title="报告模板")
        self.record_injection([(source.id, None)])
        provider = self.make_provider()
        self.assertAlmostEqual(provider.factor(source.id), 0.98)

    def test_clicked_source_is_boosted(self) -> None:
        source = self.add_note(title="报告模板")
        self.record_injection([(source.id, None)])
        self.record_click(source.id)
        self.record_click(source.id)
        provider = self.make_provider()
        self.assertAlmostEqual(provider.factor(source.id), 1.10)

    def test_factor_is_bounded(self) -> None:
        boosted = self.add_note(title="报告模板A")
        ignored = self.add_note(title="报告模板B", content="完全不同的另一段资料文本。")
        for _ in range(10):
            self.record_click(boosted.id)
        self.record_injection([(boosted.id, None)])
        for _ in range(20):
            self.record_injection([(ignored.id, None)], turn_id="turn_x")
        provider = self.make_provider()
        self.assertAlmostEqual(provider.factor(boosted.id), 1.2)
        self.assertAlmostEqual(provider.factor(ignored.id), 0.85)

    def test_non_source_citations_are_ignored(self) -> None:
        self.events.record(
            RetrievalEventKind.INJECTION,
            "随便",
            hit_counts={"memory": 1},
            detail={
                "citations": [
                    {"label": "K1", "scope": "memory", "refId": "mem_1"}
                ]
            },
        )
        self.events.record(
            RetrievalEventKind.CITATION_CLICK,
            "随便",
            detail={"label": "K1", "scope": "memory", "refId": "mem_1"},
        )
        provider = self.make_provider()
        self.assertEqual(provider.factor("mem_1"), 1.0)

    def test_cache_holds_until_ttl(self) -> None:
        source = self.add_note(title="报告模板")
        provider = self.make_provider()
        self.assertEqual(provider.factor(source.id), 1.0)
        self.record_injection([(source.id, None)])
        self.record_click(source.id)
        self.assertEqual(provider.factor(source.id), 1.0)
        self.now[0] = 61.0
        self.assertAlmostEqual(provider.factor(source.id), 1.05)

    def test_chunk_clicks_attribute_to_parent_source(self) -> None:
        first = "第一段。" + "这是文件第一段的正文内容。" * 40
        second = "第二段。" + "这是文件第二段的正文内容。" * 40
        file_source = self.knowledge.create_source(
            kind=KnowledgeSourceKind.FILE,
            origin=KnowledgeSourceOrigin.USER,
            title="长文档",
            content=f"{first}\n\n{second}",
            file_name="长文档.md",
        )
        chunk_ids = self.knowledge.list_chunk_ids(file_source.id)
        self.assertGreaterEqual(len(chunk_ids), 2)
        first_chunk = chunk_ids[0]
        self.record_injection([(first_chunk, file_source.id)])
        self.record_click(first_chunk)

        provider = self.make_provider()
        expected = 1.05
        self.assertAlmostEqual(provider.factor(first_chunk), expected)
        self.assertAlmostEqual(provider.factor(file_source.id), expected)


class SearchRankingIntegrationTest(CitationFeedbackTestCase):
    def test_feedback_flips_equal_literal_ranking(self) -> None:
        source_a = self.add_note(title="报告模板A")
        source_b = self.add_note(title="报告模板B")
        self.record_injection([(source_a.id, None), (source_b.id, None)])
        self.record_click(source_b.id)

        provider = self.make_provider()
        self.knowledge.set_feedback_provider(provider)
        grouped = self.knowledge.search("季度报告模板", [KnowledgeScope.SOURCE])
        hits = grouped[KnowledgeScope.SOURCE]

        self.assertEqual(hits[0].ref_id, source_b.id)
        self.assertGreater(hits[0].score, 0.0)
        ranked = {hit.ref_id: hit.score for hit in hits}
        self.assertGreater(ranked[source_b.id], ranked[source_a.id])

    def test_without_feedback_provider_ranking_unchanged(self) -> None:
        source_a = self.add_note(title="报告模板A")
        source_b = self.add_note(title="报告模板B")
        self.record_injection([(source_a.id, None), (source_b.id, None)])
        self.record_click(source_b.id)

        grouped = self.knowledge.search("季度报告模板", [KnowledgeScope.SOURCE])
        hits = grouped[KnowledgeScope.SOURCE]
        scores = {hit.ref_id: hit.score for hit in hits}
        self.assertAlmostEqual(scores[source_a.id], scores[source_b.id])


if __name__ == "__main__":
    unittest.main()
