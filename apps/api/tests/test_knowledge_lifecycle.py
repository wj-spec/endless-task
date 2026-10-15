"""R5.10 知识生命周期验收：重复合并提案与衰减确认。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.domain.models import (
    KnowledgeProposalStatus,
    KnowledgeProposalType,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
    RetrievalEventKind,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.knowledge.lifecycle import (
    KnowledgeLifecycleService,
    char_trigrams,
    trigram_jaccard,
)
from endless_task.proposals.budget import ProposalBudget
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
    SqliteRetrievalEventRepository,
)

COFFEE_RULE = "使用咖啡机后必须清洗奶管，否则奶路会堵塞。"
OLD_TIMESTAMP = "2026-07-01T00:00:00.000Z"
DECAY_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "lifecycle.db"
        )
        self.database.initialize()
        self.knowledge = SqliteKnowledgeRepository(self.database)
        self.proposals = SqliteKnowledgeProposalRepository(self.database)
        self.events = SqliteRetrievalEventRepository(self.database)
        self.chat = SqliteChatRepository(self.database)
        self.service = KnowledgeLifecycleService(
            knowledge_repository=self.knowledge,
            proposal_repository=self.proposals,
            retrieval_event_repository=self.events,
            chat_repository=self.chat,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def add_note(
        self,
        *,
        title: str,
        content: str,
        origin: KnowledgeSourceOrigin = KnowledgeSourceOrigin.USER,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        created_at: str | None = None,
    ):
        repository = self.knowledge
        if created_at is not None:
            repository = SqliteKnowledgeRepository(
                self.database, clock=lambda: created_at
            )
        return repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=origin,
            title=title,
            content=content,
            source_conversation_id=conversation_id,
            proposed_by_turn_id=turn_id,
        )


class TrigramMathTest(unittest.TestCase):
    def test_jaccard_bounds_and_overlap(self) -> None:
        left = char_trigrams("使用咖啡机后必须清洗奶管")
        self.assertAlmostEqual(trigram_jaccard(left, left), 1.0)
        right = char_trigrams("天气预报显示明天有雨")
        self.assertEqual(trigram_jaccard(left, right), 0.0)
        self.assertEqual(trigram_jaccard(frozenset(), left), 0.0)
        superset = char_trigrams(COFFEE_RULE)
        self.assertTrue(0.0 < trigram_jaccard(left, superset) < 1.0)


class DuplicateDetectionTest(LifecycleTestCase):
    def test_duplicate_source_creates_merge_proposal(self) -> None:
        self.chat.create_conversation()
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)

        proposal = self.service.detect_duplicates(duplicate)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertIs(proposal.proposal_type, KnowledgeProposalType.MERGE_SOURCE)
        self.assertIs(proposal.status, KnowledgeProposalStatus.PENDING)
        self.assertEqual(proposal.payload["source_id"], duplicate.id)
        self.assertEqual(proposal.payload["target_id"], original.id)
        self.assertGreaterEqual(float(proposal.payload["overlap"]), 0.5)
        self.assertIn("高度重复", str(proposal.payload["reason"]))

    def test_distinct_source_creates_no_proposal(self) -> None:
        self.chat.create_conversation()
        self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        other = self.add_note(
            title="会议室预订",
            content="会议室需提前一天预订，取消请提前两小时通知。",
        )
        self.assertIsNone(self.service.detect_duplicates(other))


class ProposalAnchorTest(LifecycleTestCase):
    """批次 B Task 1 复审 M-6：提案兜底锚点不许落到**执行会话**（系统对象）。

    `_proposal_anchor` 在来源会话/轮次缺失时取 `list_conversations()[0]`，而那是
    `ORDER BY updated_at DESC` —— 定时/提醒的到点执行刚更新过**它自己的执行会话**，
    所以不排除的话它会几乎必然排在第一位，知识提案就被锚到系统对象上。ADR-1 §5 只说
    了执行会话"参与知识生命周期枚举"，没说它能当锚点。

    红检查：把 `knowledge/lifecycle.py` 里的 `exclude_internal=True` 去掉 →
    第一条用例红（提案落到执行会话）。
    """

    def test_fallback_anchor_skips_the_execution_conversation(self) -> None:
        # 先建一份旧的普通会话（真·用户会话）
        self.chat.create_conversation()
        # 任务「执行记录」：内部执行会话（批次 B Task 1 起会由 worker 建出来）
        execution = self.chat.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_anchor",
            title="《每周发布检查》· 执行记录",
        )
        # 让执行会话成为**最近更新**的那一条（正是到点执行刚跑完的形状）
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                ("2099-01-01T00:00:00.000Z", execution.id),
            )

        newest = self.chat.list_conversations()[0]
        self.assertEqual(execution.id, newest.id, "前提：不排除时执行会话排第一")

        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(
            title="咖啡机使用规范",
            content=COFFEE_RULE + "每次使用后请记录。",
        )

        proposal = self.service.detect_duplicates(duplicate)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(original.id, proposal.payload["target_id"])
        # 兜底锚点必须是**用户**会话，不是刚被更新的执行会话
        self.assertNotEqual(execution.id, proposal.conversation_id)
        user_conversation = [
            item for item in self.chat.list_conversations()
            if item.id != execution.id
        ][0]
        self.assertEqual(user_conversation.id, proposal.conversation_id)

    def test_fallback_anchor_returns_none_when_only_execution_conversations_exist(
        self,
    ) -> None:
        """库里只有执行会话时，宁可**不锚**（返回 None → 不建提案），也不锚系统对象。"""
        execution = self.chat.create_internal_conversation(
            owner_kind="reminder_execution",
            owner_id="reminder_anchor",
            title="《整理文档》· 执行记录",
        )
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(
            title="咖啡机使用规范",
            content=COFFEE_RULE + "每次使用后请记录。",
        )

        proposal = self.service.detect_duplicates(duplicate)

        # 没有用户会话可锚 → 不建提案（而不是把提案挂到执行会话上）
        self.assertIsNone(proposal)
        self.assertEqual((), self.proposals.list_proposals_for_source(duplicate.id))
        self.assertEqual((), self.proposals.list_proposals_for_source(original.id))
        # 也没有任何提案挂到那条执行会话上
        self.assertEqual(
            (),
            self.proposals.list_proposals(conversation_id=execution.id),
        )

    def test_repeat_detection_returns_existing_pending_proposal(self) -> None:
        self.chat.create_conversation()
        self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        first = self.service.detect_duplicates(duplicate)
        second = self.service.detect_duplicates(duplicate)
        self.assertIsNotNone(first)
        self.assertEqual(first.id, second.id)
        pending = self.proposals.list_pending(limit=10)
        self.assertEqual(len(pending), 1)

    def test_expired_source_is_not_a_duplicate_target(self) -> None:
        self.chat.create_conversation()
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        self.knowledge.expire_source(original.id)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        self.assertIsNone(self.service.detect_duplicates(duplicate))

    def test_no_anchor_conversation_skips_proposal(self) -> None:
        self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        self.assertIsNone(self.service.detect_duplicates(duplicate))

    def test_accept_merge_expires_duplicate_and_keeps_target(self) -> None:
        self.chat.create_conversation()
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        proposal = self.service.detect_duplicates(duplicate)
        assert proposal is not None

        resolved, source = self.proposals.accept_proposal(proposal.id)

        self.assertIs(resolved.status, KnowledgeProposalStatus.ACCEPTED)
        self.assertEqual(resolved.resolved_source_id, duplicate.id)
        self.assertIs(source.status, KnowledgeSourceStatus.EXPIRED)
        self.assertIs(
            self.knowledge.get_source(original.id).status,
            KnowledgeSourceStatus.ACTIVE,
        )

    def test_reject_merge_keeps_both_sources(self) -> None:
        self.chat.create_conversation()
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        proposal = self.service.detect_duplicates(duplicate)
        assert proposal is not None

        self.proposals.reject_proposal(proposal.id)

        self.assertIs(
            self.knowledge.get_source(original.id).status,
            KnowledgeSourceStatus.ACTIVE,
        )
        self.assertIs(
            self.knowledge.get_source(duplicate.id).status,
            KnowledgeSourceStatus.ACTIVE,
        )

    def test_expired_duplicate_can_be_deleted(self) -> None:
        self.chat.create_conversation()
        self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        proposal = self.service.detect_duplicates(duplicate)
        assert proposal is not None
        self.proposals.accept_proposal(proposal.id)

        deleted = self.knowledge.delete_source(duplicate.id)

        self.assertIs(deleted.status, KnowledgeSourceStatus.DELETED)
        self.assertIsNone(deleted.expired_at)

    def test_accept_merge_requires_active_target(self) -> None:
        self.chat.create_conversation()
        original = self.add_note(title="咖啡机规范", content=COFFEE_RULE)
        duplicate = self.add_note(title="咖啡机使用规范", content=COFFEE_RULE)
        proposal = self.service.detect_duplicates(duplicate)
        assert proposal is not None
        self.knowledge.expire_source(original.id)

        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal.id)


class DecayConfirmationTest(LifecycleTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 固定时钟的仓储：衰减判定需要确定性的创建/处理时间。
        self.proposals = SqliteKnowledgeProposalRepository(
            self.database, clock=lambda: "2026-08-20T00:00:00.000Z"
        )
        self.events = SqliteRetrievalEventRepository(
            self.database, clock=lambda: "2026-08-20T09:00:00.000Z"
        )
        self.service = KnowledgeLifecycleService(
            knowledge_repository=self.knowledge,
            proposal_repository=self.proposals,
            retrieval_event_repository=self.events,
            chat_repository=self.chat,
        )

    def add_agent_note(
        self, *, title: str, content: str, created_at: str = OLD_TIMESTAMP
    ):
        conversation = self.chat.create_conversation()
        source = self.add_note(
            title=title,
            content=content,
            origin=KnowledgeSourceOrigin.AGENT,
            conversation_id=conversation.id,
            turn_id="turn_decay",
            created_at=created_at,
        )
        return source, conversation

    def test_stale_agent_source_receives_decay_proposal(self) -> None:
        source, conversation = self.add_agent_note(
            title="会议室预订", content="会议室预订需提前一天申请。"
        )

        proposals = self.service.check_decay(now=DECAY_NOW)

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertIs(proposal.proposal_type, KnowledgeProposalType.EXPIRE_SOURCE)
        self.assertIs(proposal.status, KnowledgeProposalStatus.PENDING)
        self.assertEqual(proposal.payload["source_id"], source.id)
        self.assertTrue(proposal.payload.get("decay"))
        self.assertEqual(proposal.conversation_id, conversation.id)
        self.assertIn("仍然有效吗", str(proposal.payload["reason"]))

    def test_recently_used_source_is_skipped(self) -> None:
        source, conversation = self.add_agent_note(
            title="会议室预订", content="会议室预订需提前一天申请。"
        )
        self.events.record(
            RetrievalEventKind.INJECTION,
            "会议室怎么订",
            hit_counts={"source": 1},
            conversation_id=conversation.id,
            turn_id="turn_used",
            detail={
                "citations": [
                    {"label": "K1", "scope": "source", "refId": source.id}
                ]
            },
        )

        self.assertEqual(self.service.check_decay(now=DECAY_NOW), ())

    def test_user_origin_source_never_decays(self) -> None:
        conversation = self.chat.create_conversation()
        self.add_note(
            title="咖啡机规范",
            content=COFFEE_RULE,
            origin=KnowledgeSourceOrigin.USER,
            conversation_id=conversation.id,
            turn_id="turn_user",
            created_at=OLD_TIMESTAMP,
        )
        self.assertEqual(self.service.check_decay(now=DECAY_NOW), ())

    def test_young_agent_source_is_skipped(self) -> None:
        self.add_agent_note(
            title="新近知识",
            content="刚记录不久的知识条目。",
            created_at="2026-08-20T00:00:00.000Z",
        )
        self.assertEqual(self.service.check_decay(now=DECAY_NOW), ())

    def test_pending_expire_proposal_suppresses_decay(self) -> None:
        source, conversation = self.add_agent_note(
            title="会议室预订", content="会议室预订需提前一天申请。"
        )
        self.proposals.create_proposal(
            conversation_id=conversation.id,
            turn_id="turn_decay",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source.id, "title": source.title,
                     "reason": "用户提到过时"},
        )
        self.assertEqual(self.service.check_decay(now=DECAY_NOW), ())

    def test_recently_reviewed_source_respects_cooldown(self) -> None:
        source, conversation = self.add_agent_note(
            title="会议室预订", content="会议室预订需提前一天申请。"
        )
        proposal = self.proposals.create_proposal(
            conversation_id=conversation.id,
            turn_id="turn_decay",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source.id, "title": source.title,
                     "reason": "上一轮衰减确认"},
        )
        self.proposals.reject_proposal(proposal.id)

        self.assertEqual(self.service.check_decay(now=DECAY_NOW), ())
        revived = self.service.check_decay(
            now=DECAY_NOW + timedelta(days=100)
        )
        self.assertEqual(len(revived), 1)

    def test_budget_exhaustion_blocks_decay(self) -> None:
        blocked = KnowledgeLifecycleService(
            knowledge_repository=self.knowledge,
            proposal_repository=self.proposals,
            retrieval_event_repository=self.events,
            chat_repository=self.chat,
            budget=ProposalBudget(self.database, daily_limit=0),
        )
        self.add_agent_note(
            title="会议室预订", content="会议室预订需提前一天申请。"
        )
        self.assertEqual(blocked.check_decay(now=DECAY_NOW), ())

    def test_per_run_cap_limits_decay_proposals(self) -> None:
        for index in range(3):
            self.add_agent_note(
                title=f"陈旧知识{index}",
                content=f"这是第{index}条长期未被引用的知识内容。",
            )
        proposals = self.service.check_decay(now=DECAY_NOW)
        self.assertEqual(len(proposals), 2)


if __name__ == "__main__":
    unittest.main()
