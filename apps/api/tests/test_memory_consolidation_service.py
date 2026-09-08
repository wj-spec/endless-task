"""B2 记忆巩固：聚类 → 提案 → 确认并入的集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import MemoryKind, MemoryStatus
from endless_task.memory import MemoryConsolidationService
from endless_task.storage import (
    Database,
    SqliteMemoryConsolidationRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)


class _ConsolidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "b2.db")
        self.database.initialize()
        self.memories = SqliteMemoryRepository(self.database)
        self.proposals = SqliteMemoryProposalRepository(self.database)
        self.consolidations = SqliteMemoryConsolidationRepository(self.database)
        self.service = MemoryConsolidationService(
            memory_repository=self.memories,
            proposal_repository=self.proposals,
            consolidation_repository=self.consolidations,
            threshold=0.5,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(
        self,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        pinned: bool = False,
        importance: float = 0.5,
        conversation_id: str = "conv_1",
    ):
        memory = self.memories.create_memory(
            kind=kind,
            content=content,
            source_conversation_id=conversation_id,
            source_turn_id="turn_1",
        )
        if importance != 0.5:
            memory = self.memories.set_memory_importance(memory.id, importance)
        if pinned:
            memory = self.memories.set_memory_pinned(memory.id, True)
        return memory


class PlanTest(_ConsolidationTestCase):
    def test_plan_finds_similar_cluster(self) -> None:
        first = self._add("用户偏好周五发布版本")
        second = self._add("用户偏好周五发布新版本")
        self._add("天气预报明天有雨")
        clusters = self.service.plan()
        self.assertEqual(1, len(clusters))
        self.assertEqual(
            {first.id, second.id}, set(clusters[0].memory_ids)
        )

    def test_pinned_memories_are_excluded(self) -> None:
        self._add("用户偏好周五发布版本", pinned=True)
        self._add("用户偏好周五发布新版本")
        self.assertEqual((), self.service.plan())

    def test_already_consolidated_clusters_are_not_replanned(self) -> None:
        self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        report = self.service.create_proposals()
        self.assertEqual(1, report.created_count if hasattr(report, "created_count") else len(report.created))
        self.assertEqual((), self.service.plan())


class CreateProposalsTest(_ConsolidationTestCase):
    def test_proposal_and_trace_are_created(self) -> None:
        first = self._add("用户偏好周五发布版本")
        second = self._add("用户偏好周五发布新版本")
        report = self.service.create_proposals()
        self.assertEqual(1, len(report.created))
        candidate = report.created[0]
        self.assertEqual("fact", candidate.proposal.kind.value)
        self.assertIn("巩固", candidate.proposal.reason)
        self.assertIn("用户偏好周五发布", candidate.proposal.content)
        record = self.consolidations.find_by_proposal(candidate.proposal.id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual("pending", record.status)
        self.assertEqual({first.id, second.id}, set(record.source_memory_ids))

    def test_same_cluster_is_not_proposed_twice(self) -> None:
        self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        self.assertEqual(1, len(self.service.create_proposals().created))
        self.assertEqual(0, len(self.service.create_proposals().created))
        self.assertEqual(1, len(self.consolidations.list_records()))

    def test_max_clusters_per_run_is_respected(self) -> None:
        service = MemoryConsolidationService(
            memory_repository=self.memories,
            proposal_repository=self.proposals,
            consolidation_repository=self.consolidations,
            threshold=0.5,
            max_clusters_per_run=1,
        )
        self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        self._add("用户喜欢深色主题界面")
        self._add("用户喜欢深色主题的界面")
        self.assertEqual(1, len(service.create_proposals().created))

    def test_report_json_shape(self) -> None:
        self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        payload = self.service.create_proposals().as_json()
        self.assertEqual(1, payload["createdCount"])
        self.assertIn("memoryIds", payload["created"][0])


class FinalizeTest(_ConsolidationTestCase):
    def test_accept_marks_sources_merged_and_links_insight(self) -> None:
        first = self._add("用户偏好周五发布版本")
        second = self._add("用户偏好周五发布新版本")
        report = self.service.create_proposals()
        proposal = report.created[0].proposal
        _, insight = self.proposals.accept_proposal(proposal.id)
        merged = self.service.finalize(proposal.id, insight)
        assert merged is not None
        self.assertEqual({first.id, second.id}, set(merged))
        for memory_id in (first.id, second.id):
            stored = self.memories.get_memory(memory_id)
            self.assertEqual(MemoryStatus.EXPIRED, stored.status)
            self.assertEqual("consolidated", stored.expired_reason)
            self.assertEqual(insight.id, stored.superseded_by)
        record = self.consolidations.find_by_proposal(proposal.id)
        assert record is not None
        self.assertEqual("accepted", record.status)
        self.assertEqual(insight.id, record.insight_memory_id)

    def test_insight_inherits_top_importance(self) -> None:
        self._add("用户偏好周五发布版本", importance=0.9)
        self._add("用户偏好周五发布新版本", importance=0.5)
        report = self.service.create_proposals()
        proposal = report.created[0].proposal
        _, insight = self.proposals.accept_proposal(proposal.id)
        self.service.finalize(proposal.id, insight)
        self.assertEqual(0.9, self.memories.get_memory(insight.id).importance)

    def test_finalize_is_idempotent(self) -> None:
        self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        report = self.service.create_proposals()
        proposal = report.created[0].proposal
        _, insight = self.proposals.accept_proposal(proposal.id)
        self.service.finalize(proposal.id, insight)
        self.assertEqual((), self.service.finalize(proposal.id, insight))

    def test_reject_keeps_memories_active_and_blocks_reproposal(self) -> None:
        first = self._add("用户偏好周五发布版本")
        self._add("用户偏好周五发布新版本")
        report = self.service.create_proposals()
        proposal = report.created[0].proposal
        self.proposals.reject_proposal(proposal.id)
        self.assertIsNotNone(self.service.reject(proposal.id))
        self.assertEqual(
            MemoryStatus.ACTIVE, self.memories.get_memory(first.id).status
        )
        self.assertEqual(0, len(self.service.create_proposals().created))

    def test_finalize_without_record_returns_none(self) -> None:
        memory = self._add("普通记忆")
        self.assertIsNone(self.service.finalize("mprop_missing", memory))


if __name__ == "__main__":
    unittest.main()
