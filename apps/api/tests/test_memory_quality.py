from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_v2 import MemoryScope
from endless_task.runtime_v2.memory_quality import RuntimeV2MemoryQualityService
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2MemoryRepository,
)


class FakeEmbedder:
    """字符分布向量的确定性 embedder：相似文本产生高相似度向量。"""

    model_name = "fake"

    def __init__(self) -> None:
        self.ready = False
        self.calls = 0

    def ensure_ready(self) -> None:
        self.ready = True

    def embed_batch(self, texts):
        self.calls += 1
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str):
        vec = [0.0] * 64
        for ch in text:
            vec[hash(ch) % 64] += 1.0
        norm = math.sqrt(sum(value * value for value in vec)) or 1.0
        return [value / norm for value in vec]


class FailingEmbedder(FakeEmbedder):
    def embed_batch(self, texts):
        from endless_task.knowledge.embeddings import EmbeddingError

        raise EmbeddingError("boom")


class MemoryQualityBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "quality.db")
        self.database.initialize()
        self.repository = SqliteRuntimeV2MemoryRepository(self.database)
        self.chat_repository = SqliteChatRepository(self.database)
        self.conversation_id = self.chat_repository.create_conversation().id

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _add(
        self,
        content: str,
        *,
        scope=MemoryScope.USER_GLOBAL,
        expires_at=None,
    ):
        return self.repository.create_memory(
            scope=scope,
            kind="fact",
            content=content,
            conversation_id=self.conversation_id,
            expires_at=expires_at,
        )


class MemoryQualityServiceTest(MemoryQualityBase):
    def test_exact_duplicate_is_found_without_embedder(self) -> None:
        self._add("用户喜欢简洁回答。")
        service = RuntimeV2MemoryQualityService(memory_repository=self.repository)
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户喜欢简洁回答。",
        )
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate.content, "用户喜欢简洁回答。")

    def test_semantic_duplicate_is_found_with_embedder(self) -> None:
        self._add("用户偏好简洁的回答方式。")
        embedder = FakeEmbedder()
        service = RuntimeV2MemoryQualityService(
            memory_repository=self.repository,
            embedder=embedder,
            similarity_threshold=0.5,
        )
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户喜欢简洁回答。",
        )
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate.content, "用户偏好简洁的回答方式。")

    def test_distinct_content_is_not_duplicate(self) -> None:
        self._add("用户在北京工作。")
        embedder = FakeEmbedder()
        service = RuntimeV2MemoryQualityService(
            memory_repository=self.repository,
            embedder=embedder,
            similarity_threshold=0.9,
        )
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户养了一只猫。",
        )
        self.assertIsNone(duplicate)

    def test_exclude_id_skips_target(self) -> None:
        memory = self._add("用户喜欢简洁回答。")
        service = RuntimeV2MemoryQualityService(memory_repository=self.repository)
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户喜欢简洁回答。",
            exclude_id=memory.id,
        )
        self.assertIsNone(duplicate)

    def test_embedding_failure_degrades_to_literal(self) -> None:
        self._add("用户偏好简洁的回答方式。")
        service = RuntimeV2MemoryQualityService(
            memory_repository=self.repository,
            embedder=FailingEmbedder(),
            similarity_threshold=0.5,
        )
        # 语义路径失败:降级后仅字面匹配,不同文本不命中。
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户喜欢简洁回答。",
        )
        self.assertIsNone(duplicate)
        exact = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户偏好简洁的回答方式。",
        )
        self.assertIsNotNone(exact)

    def test_expired_memory_is_not_candidate(self) -> None:
        self._add("用户喜欢简洁回答。", expires_at="2000-01-01T00:00:00Z")
        service = RuntimeV2MemoryQualityService(memory_repository=self.repository)
        duplicate = service.find_duplicate(
            conversation_id=self.conversation_id,
            content="用户喜欢简洁回答。",
        )
        self.assertIsNone(duplicate)


class MemoryExpiryTest(MemoryQualityBase):
    def test_future_expiry_keeps_memory_visible(self) -> None:
        self._add("用户周六开会。", expires_at="2999-01-01T00:00:00Z")
        visible = self.repository.list_active_memories_content(self.conversation_id)
        self.assertEqual(len(visible), 1)

    def test_past_expiry_hides_memory_and_expire_soft_deletes(self) -> None:
        self._add("用户周六开会。", expires_at="2000-01-01T00:00:00Z")
        visible = self.repository.list_active_memories_content(self.conversation_id)
        self.assertEqual(visible, ())

        expired = self.repository.expire_overdue_memories()
        self.assertEqual(expired, 1)
        # 过期记忆被软删。
        memory = self.repository.find_active_user_global_memory(
            conversation_id=self.conversation_id,
            content="用户周六开会。",
        )
        self.assertIsNone(memory)

    def test_visible_memories_query_filters_expired(self) -> None:
        self._add("永久记忆。")
        self._add("临时记忆。", expires_at="2000-01-01T00:00:00Z")
        records = self.repository.list_visible_memories(
            conversation_id=self.conversation_id,
            workspace_id=None,
            lane_id="lane_1",
        )
        contents = [record.content for record in records]
        self.assertIn("永久记忆。", contents)
        self.assertNotIn("临时记忆。", contents)


if __name__ == "__main__":
    unittest.main()
