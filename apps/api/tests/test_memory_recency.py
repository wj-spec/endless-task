"""B1 记忆时间衰减：近期性在检索与记忆召回里的落地。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.runtime_v2 import Actor, MemoryScope, TranscriptEntryType
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteKnowledgeRepository,
    SqliteRuntimeV2MemoryRepository,
    SqliteRuntimeV2Repository,
)
from endless_task.runtime_v2.memory import RuntimeV2MemoryService

BASE = datetime(2026, 1, 31, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (BASE - timedelta(days=days_ago)).isoformat()


class _Clock:
    """可推进的测试时钟。"""

    def __init__(self, current: datetime = BASE) -> None:
        self.current = current

    def __call__(self) -> str:
        return self.current.isoformat()

    def at(self, days_ago: float) -> None:
        self.current = BASE - timedelta(days=days_ago)


class KnowledgeRecencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[tempfile.TemporaryDirectory] = []

    def tearDown(self) -> None:
        for directory in self._dirs:
            directory.cleanup()

    def _fresh_database(self) -> Database:
        directory = tempfile.TemporaryDirectory()
        self._dirs.append(directory)
        database = Database(Path(directory.name) / "recency.db")
        database.initialize()
        return database

    def _repository(self, *, recency_weight: float, tau_days: float = 30.0):
        clock = _Clock()
        repository = SqliteKnowledgeRepository(
            self._fresh_database(),
            clock=clock,
            hybrid_literal_weight=0.4,
            hybrid_semantic_weight=0.6,
            recency_weight=recency_weight,
            decay_tau_days=tau_days,
        )
        return repository, clock

    def _add(self, repository, clock, *, title: str, content: str, days_ago: float):
        clock.at(days_ago)
        return repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
        )

    def test_newer_source_wins_when_recency_enabled(self) -> None:
        repository, clock = self._repository(recency_weight=1.0)
        old = self._add(
            repository,
            clock,
            title="咖啡机规范",
            content="清洗奶管 清洗奶管 清洗奶管",
            days_ago=365,
        )
        new = self._add(
            repository,
            clock,
            title="咖啡机规范（新版）",
            content="清洗奶管",
            days_ago=0,
        )
        clock.at(0)
        hits = repository.search("清洗奶管", [KnowledgeScope.SOURCE])
        self.assertEqual(
            [new.id, old.id],
            [hit.ref_id for hit in hits[KnowledgeScope.SOURCE]],
        )

    def test_pure_relevance_ignores_time(self) -> None:
        repository, clock = self._repository(recency_weight=0.0)
        old = self._add(
            repository,
            clock,
            title="咖啡机规范",
            content="清洗奶管 清洗奶管 清洗奶管",
            days_ago=365,
        )
        self._add(
            repository,
            clock,
            title="咖啡机规范（新版）",
            content="清洗奶管",
            days_ago=0,
        )
        clock.at(0)
        hits = repository.search("清洗奶管", [KnowledgeScope.SOURCE])
        # λ=1（recency_weight=0）：命中次数多的旧源仍然在前，分数保持旧口径。
        self.assertEqual(old.id, hits[KnowledgeScope.SOURCE][0].ref_id)
        self.assertEqual(6.0, hits[KnowledgeScope.SOURCE][0].score)

    def test_larger_tau_flattens_recency(self) -> None:
        """τ 越小衰减越快：小 τ 让新源反超，大 τ 让高相关的旧源保持领先。"""
        fast, fast_clock = self._repository(recency_weight=0.5, tau_days=1)
        old_fast = self._add(
            fast, fast_clock, title="旧", content="关键词 关键词 关键词", days_ago=10
        )
        new_fast = self._add(fast, fast_clock, title="新", content="关键词", days_ago=0)
        fast_clock.at(0)
        fast_hits = fast.search("关键词", [KnowledgeScope.SOURCE])[
            KnowledgeScope.SOURCE
        ]
        self.assertEqual(new_fast.id, fast_hits[0].ref_id)

        slow, slow_clock = self._repository(recency_weight=0.5, tau_days=3650)
        old_slow = self._add(
            slow, slow_clock, title="旧", content="关键词 关键词 关键词", days_ago=10
        )
        self._add(slow, slow_clock, title="新", content="关键词", days_ago=0)
        slow_clock.at(0)
        slow_hits = slow.search("关键词", [KnowledgeScope.SOURCE])[
            KnowledgeScope.SOURCE
        ]
        self.assertEqual(old_slow.id, slow_hits[0].ref_id)
        self.assertNotEqual(old_fast.id, fast_hits[0].ref_id)

    def test_expired_and_deleted_still_excluded(self) -> None:
        repository, clock = self._repository(recency_weight=1.0)
        expired = self._add(
            repository, clock, title="过期规范", content="清洗奶管", days_ago=0
        )
        repository.expire_source(expired.id)
        deleted = self._add(
            repository, clock, title="已删除规范", content="清洗奶管", days_ago=0
        )
        repository.delete_source(deleted.id)
        clock.at(0)
        hits = repository.search("清洗奶管", [KnowledgeScope.SOURCE])
        self.assertEqual([], hits.get(KnowledgeScope.SOURCE, []))


class MemoryRecallRecencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "memory.db")
        self.database.initialize()
        self.clock = _Clock()
        self.chat_repository = SqliteChatRepository(self.database)
        self.runtime_repository = SqliteRuntimeV2Repository(self.database)
        self.memory_repository = SqliteRuntimeV2MemoryRepository(
            self.database, clock=self.clock
        )
        self.conversation = self.chat_repository.create_conversation()
        self.lane = self.runtime_repository.create_lane(
            conversation_id=self.conversation.id
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_memory(self, content: str, *, days_ago: float):
        self.clock.at(days_ago)
        return self.memory_repository.create_memory(
            conversation_id=self.conversation.id,
            scope=MemoryScope.CONVERSATION_TREE,
            kind="fact",
            content=content,
        )

    def test_visible_memories_are_ordered_newest_first(self) -> None:
        old = self._add_memory("很久以前的事。", days_ago=100)
        middle = self._add_memory("上个月的事。", days_ago=30)
        new = self._add_memory("今天的事。", days_ago=0)
        self.clock.at(0)
        records = self.memory_repository.list_visible_memories(
            conversation_id=self.conversation.id,
            workspace_id=None,
            lane_id=self.lane.id,
        )
        self.assertEqual(
            [new.id, middle.id, old.id],
            [record.id for record in records],
        )

    def test_context_injection_keeps_recent_memories_when_over_budget(self) -> None:
        service = RuntimeV2MemoryService(
            chat_repository=self.chat_repository,
            runtime_repository=self.runtime_repository,
            memory_repository=self.memory_repository,
        )
        # 超过 _MAX_CONTEXT_MEMORIES(20)：应当保留最近 20 条，而不是最早 20 条。
        for index in range(25):
            self._add_memory(f"记忆 {index}", days_ago=100 - index)
        self.clock.at(0)
        run = self.runtime_repository.create_run(
            conversation_id=self.conversation.id,
            lane_id=self.lane.id,
            trigger_entry_id=self.runtime_repository.append_entry(
                conversation_id=self.conversation.id,
                lane_id=self.lane.id,
                type=TranscriptEntryType.USER_MESSAGE,
                actor=Actor.USER,
                payload={"content": "任务"},
                context_policy={"include_in_llm": True, "transform": "full"},
            ).id,
        )
        messages = service.context_messages(
            conversation_id=self.conversation.id,
            lane_id=self.lane.id,
            run_id=run.id,
        )
        self.assertEqual(1, len(messages))
        content = messages[0].content
        self.assertIn("记忆 24", content)
        self.assertNotIn("记忆 0", content)


if __name__ == "__main__":
    unittest.main()
