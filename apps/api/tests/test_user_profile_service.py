"""B5 用户画像服务：缓存友好的刷新策略与手写覆盖。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.memory import UserProfileService
from endless_task.runtime_v2 import MemoryScope
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2MemoryRepository,
    SqliteRuntimeV2Repository,
    SqliteUserProfileRepository,
)

BASE = datetime(2026, 1, 31, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.current = BASE

    def __call__(self) -> str:
        return self.current.isoformat()

    def advance(self, **kwargs) -> None:
        self.current = self.current + timedelta(**kwargs)


class _ProfileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "b5.db")
        self.database.initialize()
        self.clock = _Clock()
        self.chat = SqliteChatRepository(self.database)
        self.runtime = SqliteRuntimeV2Repository(self.database)
        self.memories = SqliteRuntimeV2MemoryRepository(self.database)
        self.profiles = SqliteUserProfileRepository(
            self.database, clock=self.clock
        )
        self.service = UserProfileService(
            profile_repository=self.profiles,
            memory_repository=self.memories,
            clock=self.clock,
            min_refresh_seconds=300,
        )
        self.conversation = self.chat.create_conversation()
        self.lane = self.runtime.create_lane(
            conversation_id=self.conversation.id
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_memory(self, content: str, *, importance: float = 0.5):
        # v2 记忆没有重要性列，importance 参数保留以兼容用例签名。
        del importance
        return self.memories.create_memory(
            scope=MemoryScope.USER_GLOBAL,
            kind="preference",
            content=content,
            conversation_id=self.conversation.id,
        )


class RefreshTest(_ProfileTestCase):
    def test_refresh_builds_block_from_memories(self) -> None:
        self._add_memory("喜欢简洁回答", importance=0.9)
        self._add_memory("主要用中文")
        result = self.service.refresh()
        self.assertTrue(result.refreshed)
        self.assertTrue(result.version_changed)
        self.assertEqual(1, result.block.version)
        self.assertIn("- 喜欢简洁回答", result.block.content)
        self.assertIn("- 主要用中文", result.block.content)

    def test_unchanged_memories_do_not_bump_version(self) -> None:
        self._add_memory("喜欢简洁回答")
        first = self.service.refresh()
        self.clock.advance(minutes=30)
        second = self.service.refresh()
        self.assertFalse(second.refreshed)
        self.assertFalse(second.version_changed)
        self.assertEqual(first.block.version, second.block.version)
        self.assertEqual(first.block.signature, second.block.signature)
        self.assertEqual("unchanged", second.reason)

    def test_new_memory_within_interval_is_throttled(self) -> None:
        self._add_memory("喜欢简洁回答")
        self.service.refresh()
        self._add_memory("偏好周五发布")
        self.clock.advance(seconds=30)
        throttled = self.service.refresh()
        self.assertFalse(throttled.refreshed)
        # 版本与内容都保持旧值 → 前缀缓存不被破坏。
        self.assertNotIn("周五发布", throttled.block.content)

    def test_new_memory_after_interval_refreshes_and_bumps(self) -> None:
        self._add_memory("喜欢简洁回答")
        first = self.service.refresh()
        self._add_memory("偏好周五发布")
        self.clock.advance(minutes=10)
        second = self.service.refresh()
        self.assertTrue(second.refreshed)
        self.assertTrue(second.version_changed)
        self.assertEqual(first.block.version + 1, second.block.version)
        self.assertIn("周五发布", second.block.content)

    def test_force_refreshes_within_interval(self) -> None:
        self._add_memory("喜欢简洁回答")
        self.service.refresh()
        self._add_memory("偏好周五发布")
        self.clock.advance(seconds=10)
        forced = self.service.refresh(force=True)
        self.assertTrue(forced.refreshed)
        self.assertIn("周五发布", forced.block.content)

    def test_block_is_empty_without_memories(self) -> None:
        result = self.service.refresh()
        self.assertFalse(result.version_changed)
        self.assertTrue(result.block.empty)
        self.assertEqual("", self.service.block_for().content)


class EnsureCurrentTest(_ProfileTestCase):
    """运行前对齐：内容未变不写；有行消失立刻更新（宁可一次缓存失效）。"""

    def test_unchanged_memories_do_not_write(self) -> None:
        self._add_memory("喜欢简洁回答")
        first = self.service.ensure_current()
        self.assertEqual(1, first.version)
        updated_at = self.profiles.get("general").updated_at
        self.clock.advance(seconds=5)
        second = self.service.ensure_current()
        self.assertEqual(first.version, second.version)
        self.assertEqual(updated_at, self.profiles.get("general").updated_at)

    def test_added_memory_is_throttled(self) -> None:
        self._add_memory("喜欢简洁回答")
        self.service.ensure_current()
        self._add_memory("偏好周五发布")
        self.clock.advance(seconds=10)
        block = self.service.ensure_current()
        self.assertNotIn("周五发布", block.content)

    def test_removed_line_updates_immediately(self) -> None:
        memory = self._add_memory("用户住在上海。")
        self.service.ensure_current()
        replacement = self._add_memory("用户住在杭州。")
        self.memories.supersede_memory(memory.id, superseded_by=replacement.id)
        # 即使间隔未到，消失的行也必须立刻从画像里去掉。
        self.clock.advance(seconds=5)
        block = self.service.ensure_current()
        self.assertNotIn("上海", block.content)

    def test_manual_profile_is_not_touched_by_alignment(self) -> None:
        self._add_memory("记忆里的偏好")
        self.service.set_manual("- 手写画像")
        self.clock.advance(seconds=5)
        block = self.service.ensure_current()
        self.assertEqual(("- 手写画像",), block.lines)


class ManualProfileTest(_ProfileTestCase):
    def test_manual_content_is_used_and_not_overwritten(self) -> None:
        self._add_memory("记忆里的偏好")
        written = self.service.set_manual("- 我是资深工程师\n- 请直接给结论")
        self.assertTrue(written.block.manual)
        self.assertIn("我是资深工程师", written.block.content)
        self.assertIn("请直接给结论", written.block.content)

        self._add_memory("后来新增的记忆")
        self.clock.advance(minutes=30)
        kept = self.service.refresh()
        self.assertFalse(kept.refreshed)
        self.assertEqual("manual_profile_kept", kept.reason)
        self.assertNotIn("后来新增的记忆", kept.block.content)

    def test_force_rebuild_overrides_manual(self) -> None:
        self._add_memory("记忆里的偏好")
        self.service.set_manual("- 手写内容")
        forced = self.service.refresh(force=True)
        self.assertTrue(forced.refreshed)
        self.assertIn("记忆里的偏好", forced.block.content)
        self.assertNotIn("手写内容", forced.block.content)

    def test_manual_lines_are_normalized(self) -> None:
        result = self.service.set_manual("喜欢中文\n- 已有前缀")
        self.assertEqual(("- 喜欢中文", "- 已有前缀"), result.block.lines)

    def test_manual_blank_content_clears_profile(self) -> None:
        self.service.set_manual("- 内容")
        cleared = self.service.set_manual("   ")
        self.assertTrue(cleared.block.empty)


if __name__ == "__main__":
    unittest.main()
