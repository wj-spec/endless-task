"""B3 重要性加权遗忘：仓储字段、访问计数与遗忘服务的集成。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.domain.models import MemoryKind, MemoryStatus
from endless_task.memory import MemoryForgettingService
from endless_task.storage import Database, SqliteMemoryRepository

NOW = datetime(2026, 1, 31, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class _MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "memory.db")
        self.database.initialize()
        self.clock_value = _iso(0)
        self.repository = SqliteMemoryRepository(
            self.database, clock=lambda: self.clock_value
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(
        self,
        content: str,
        *,
        days_ago: float = 0.0,
        importance: float = 0.5,
        access_count: int = 0,
        pinned: bool = False,
    ):
        self.clock_value = _iso(days_ago)
        memory = self.repository.create_memory(
            kind=MemoryKind.FACT,
            content=content,
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        if importance != 0.5:
            memory = self.repository.set_memory_importance(memory.id, importance)
        if access_count:
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE memories SET access_count = ? WHERE id = ?",
                    (access_count, memory.id),
                )
            memory = self.repository.get_memory(memory.id)
        if pinned:
            memory = self.repository.set_memory_pinned(memory.id, True)
        self.clock_value = _iso(0)
        return memory


class MemoryFieldTest(_MemoryTestCase):
    def test_defaults_are_neutral(self) -> None:
        memory = self._add("默认记忆")
        self.assertEqual(0.5, memory.importance)
        self.assertEqual(0, memory.access_count)
        self.assertIsNone(memory.last_accessed_at)
        self.assertFalse(memory.pinned)

    def test_importance_and_pin_round_trip(self) -> None:
        memory = self._add("重要记忆")
        updated = self.repository.set_memory_importance(memory.id, 0.95)
        self.assertEqual(0.95, updated.importance)
        pinned = self.repository.set_memory_pinned(memory.id, True)
        self.assertTrue(pinned.pinned)
        # 越界值被夹紧而不是报错。
        self.assertEqual(1.0, self.repository.set_memory_importance(memory.id, 5).importance)

    def test_access_counting_is_throttled(self) -> None:
        memory = self._add("常用记忆")
        self.assertEqual(1, self.repository.record_access([memory.id]))
        # 1 小时内的重复注入不再计数（间隔重复语义）。
        self.assertEqual(0, self.repository.record_access([memory.id]))
        after = self.repository.get_memory(memory.id)
        self.assertEqual(1, after.access_count)
        self.assertIsNotNone(after.last_accessed_at)

        # 超过间隔后再注入一次（把时钟推后 2 小时）。
        self.clock_value = (NOW + timedelta(hours=2)).isoformat()
        self.assertEqual(1, self.repository.record_access([memory.id]))
        self.assertEqual(2, self.repository.get_memory(memory.id).access_count)

    def test_context_listing_orders_by_keep_value(self) -> None:
        normal = self._add("普通", days_ago=0)
        important = self._add("重要", days_ago=0, importance=0.9)
        pinned = self._add("钉住", days_ago=0, pinned=True)
        used = self._add("常用", days_ago=0, access_count=5)
        records = self.repository.list_memories_for_context(4)
        self.assertEqual(
            [pinned.id, important.id, used.id, normal.id],
            [record.id for record in records],
        )


class ForgettingServiceTest(_MemoryTestCase):
    def _service(self, *, max_per_run: int = 10) -> MemoryForgettingService:
        return MemoryForgettingService(
            memory_repository=self.repository,
            clock=lambda: NOW,
            max_per_run=max_per_run,
        )

    def test_preview_lists_forgettable_without_touching_data(self) -> None:
        stale = self._add("久远且不重要", days_ago=365, importance=0.0)
        self._add("新鲜", days_ago=0, importance=0.0)
        report = self._service().preview()
        self.assertTrue(report.dry_run)
        self.assertEqual([stale.id], [record.id for record in report.forgotten])
        self.assertEqual(MemoryStatus.ACTIVE, self.repository.get_memory(stale.id).status)

    def test_run_expires_low_value_memories(self) -> None:
        stale = self._add("久远且不重要", days_ago=365, importance=0.0)
        report = self._service().run()
        self.assertFalse(report.dry_run)
        self.assertEqual(1, report.forgotten_count)
        expired = self.repository.get_memory(stale.id)
        self.assertEqual(MemoryStatus.EXPIRED, expired.status)
        self.assertEqual("forgotten_low_value", expired.expired_reason)

    def test_pinned_memory_is_never_forgotten(self) -> None:
        pinned = self._add(
            "钉住的久远记忆", days_ago=999, importance=0.0, pinned=True
        )
        report = self._service().run()
        self.assertEqual(0, report.forgotten_count)
        self.assertEqual(MemoryStatus.ACTIVE, self.repository.get_memory(pinned.id).status)

    def test_important_memory_needs_review_instead_of_deletion(self) -> None:
        important = self._add("重要但久远", days_ago=999, importance=0.95)
        report = self._service().run()
        self.assertEqual(0, report.forgotten_count)
        self.assertEqual(1, len(report.needs_review))
        self.assertEqual(important.id, report.needs_review[0].record.id)
        self.assertTrue(report.as_json()["needsReview"][0]["needsReview"])
        # 重要记忆不会出现在"将被遗忘"列表里。
        self.assertEqual([], report.as_json()["forgotten"])
        # 重要记忆仍是 active，用户还能钉住它。
        self.assertEqual(
            MemoryStatus.ACTIVE, self.repository.get_memory(important.id).status
        )

    def test_frequently_used_memory_survives(self) -> None:
        self._add("常用", days_ago=365, importance=0.0, access_count=20)
        report = self._service().run()
        self.assertEqual(0, report.forgotten_count)

    def test_max_per_run_caps_deletions(self) -> None:
        for index in range(5):
            self._add(f"久远 {index}", days_ago=365 + index, importance=0.0)
        report = self._service(max_per_run=2).run()
        self.assertEqual(2, report.forgotten_count)

    def test_report_json_shape(self) -> None:
        self._add("久远", days_ago=365, importance=0.0)
        payload = self._service().run().as_json()
        self.assertIn("forgotten", payload)
        self.assertIn("forgottenCount", payload)
        self.assertFalse(payload["dryRun"])


if __name__ == "__main__":
    unittest.main()
