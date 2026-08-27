"""R5.8 EmbeddingIndexer：写时增量索引、删除清理、全量重建与语义评分。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ArtifactKind,
    FinishReason,
    MemoryKind,
)
from endless_task.knowledge import EmbeddingIndexer
from endless_task.knowledge.embeddings import EmbeddingError
from endless_task.storage import (
    Database,
    SqliteArtifactRepository,
    SqliteChatRepository,
    SqliteEmbeddingRepository,
    SqliteKnowledgeRepository,
    SqliteMemoryRepository,
)
from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)


class FakeEmbedder:
    def __init__(self, model_name: str = "fake-embed-1") -> None:
        self.model_name = model_name
        self.calls: list = []
        self.fail_ready = False

    def ensure_ready(self) -> None:
        if self.fail_ready:
            raise EmbeddingError("backend down")

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [[float(len(text) % 7), 1.0] for text in texts]


class EmbeddingIndexerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "indexer.db")
        self.database.initialize()
        self.knowledge_repository = SqliteKnowledgeRepository(self.database)
        self.memory_repository = SqliteMemoryRepository(self.database)
        self.artifact_repository = SqliteArtifactRepository(self.database)
        self.chat_repository = SqliteChatRepository(self.database)
        self.embeddings = SqliteEmbeddingRepository(self.database)
        self.indexer: EmbeddingIndexer | None = None

    def tearDown(self) -> None:
        if self.indexer is not None:
            self.indexer.stop()
        self._temporary_directory.cleanup()

    def _make_indexer(self, embedder=None, **kwargs) -> EmbeddingIndexer:
        indexer = EmbeddingIndexer(
            self.database,
            embedder or FakeEmbedder(),
            self.knowledge_repository,
            **kwargs,
        )
        self.indexer = indexer
        return indexer

    def _wait_for(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for condition")

    def _seed_completed_turn(self, user_text: str, assistant_text: str) -> str:
        conversation = self.chat_repository.create_conversation()
        snapshot = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="req-1",
            content=user_text,
        )
        variant = snapshot.response_variants[0].variant
        self.chat_repository.mark_response_running(
            turn_id=snapshot.turn.id,
            variant_id=variant.id,
        )
        self.chat_repository.complete_response(
            turn_id=snapshot.turn.id,
            variant_id=variant.id,
            content=assistant_text,
            finish_reason=FinishReason.STOP,
        )
        return snapshot.turn.id

    def test_submit_indexes_source_asynchronously(self) -> None:
        indexer = self._make_indexer()
        self.knowledge_repository.set_embedding_hook(indexer)
        indexer.start()
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        self._wait_for(
            lambda: self.embeddings.get_blob("source", source.id, "fake-embed-1")
            is not None
        )
        blob = self.embeddings.get_blob("source", source.id, "fake-embed-1")
        self.assertEqual(len(blob), 8)  # 2 个 float32

    def test_delete_source_removes_embeddings(self) -> None:
        indexer = self._make_indexer()
        self.knowledge_repository.set_embedding_hook(indexer)
        indexer.start()
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        self._wait_for(
            lambda: self.embeddings.get_blob("source", source.id, "fake-embed-1")
            is not None
        )
        self.knowledge_repository.delete_source(source.id)
        self.assertIsNone(
            self.embeddings.get_blob("source", source.id, "fake-embed-1")
        )

    def test_memory_and_artifact_hooks(self) -> None:
        indexer = self._make_indexer()
        self.memory_repository.set_embedding_hook(indexer)
        self.artifact_repository.set_embedding_hook(indexer)
        indexer.start()
        memory = self.memory_repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户喜欢喝绿茶",
            source_conversation_id="conv_x",
            source_turn_id="turn_x",
        )
        snapshot = self.artifact_repository.create_artifact(
            title="周报",
            kind=ArtifactKind.MARKDOWN,
            content="# 本周进展",
            source_conversation_id="conv_x",
            source_turn_id="turn_x",
        )
        self._wait_for(
            lambda: self.embeddings.get_blob("memory", memory.id, "fake-embed-1")
            is not None
        )
        self._wait_for(
            lambda: self.embeddings.get_blob(
                "artifact", snapshot.artifact.id, "fake-embed-1"
            )
            is not None
        )

    def test_rebuild_covers_all_scopes_and_purges_old_model(self) -> None:
        indexer = self._make_indexer()
        self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        self.memory_repository.create_memory(
            kind=MemoryKind.FACT,
            content="用户在杭州工作",
            source_conversation_id="conv_x",
            source_turn_id="turn_x",
        )
        self.artifact_repository.create_artifact(
            title="周报",
            kind=ArtifactKind.MARKDOWN,
            content="# 本周进展",
            source_conversation_id="conv_x",
            source_turn_id="turn_x",
        )
        self._seed_completed_turn("帮我查一下咖啡机保养", "需要定期清洗奶管。")

        stats = indexer.rebuild()
        self.assertEqual(
            stats, {"source": 1, "memory": 1, "artifact": 1, "conversation": 1}
        )

        second = EmbeddingIndexer(
            self.database,
            FakeEmbedder(model_name="fake-embed-2"),
            self.knowledge_repository,
        )
        self.indexer = second
        second.rebuild()
        self.assertEqual(second.stats()["models"], ["fake-embed-2"])

    def test_rebuild_excludes_invisible_entities(self) -> None:
        indexer = self._make_indexer()
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        self.knowledge_repository.expire_source(source.id)
        stats = indexer.rebuild()
        self.assertEqual(stats["source"], 0)
        self.assertIsNone(
            self.embeddings.get_blob("source", source.id, "fake-embed-1")
        )

    def test_embed_query_and_score_refs(self) -> None:
        indexer = self._make_indexer()
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        indexer.rebuild()
        query_vector = indexer.embed_query("任意查询")
        self.assertIsNotNone(query_vector)
        scores = indexer.score_refs(
            KnowledgeScope.SOURCE, query_vector, [source.id]
        )
        self.assertIn(source.id, scores)
        self.assertGreater(scores[source.id], 0.0)
        self.assertEqual(
            indexer.score_refs(KnowledgeScope.SOURCE, query_vector, []), {}
        )

    def test_unavailable_backend_disables_indexing(self) -> None:
        embedder = FakeEmbedder()
        embedder.fail_ready = True
        indexer = self._make_indexer(embedder)
        self.knowledge_repository.set_embedding_hook(indexer)
        indexer.start()
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        self._wait_for(lambda: indexer.unavailable)
        time.sleep(0.2)
        self.assertIsNone(
            self.embeddings.get_blob("source", source.id, "fake-embed-1")
        )
        self.assertIsNone(indexer.embed_query("任意查询"))

    def test_embed_text_is_truncated(self) -> None:
        indexer = self._make_indexer(max_chars=6)
        self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="长文",
            content="这是一段远远超过限制的很长很长的内容",
        )
        indexer.rebuild()
        self.assertTrue(all(len(text) <= 6 for call in indexer._embedder.calls for text in call))

    def test_dedupe_pending_jobs(self) -> None:
        embedder = FakeEmbedder()
        indexer = self._make_indexer(embedder)
        source = self.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后要清洗奶管",
        )
        indexer.submit("source", source.id)
        indexer.submit("source", source.id)
        indexer.start()
        self._wait_for(
            lambda: self.embeddings.get_blob("source", source.id, "fake-embed-1")
            is not None
        )
        indexer.stop()
        self.indexer = None
        total_texts = sum(len(call) for call in embedder.calls)
        self.assertEqual(total_texts, 1)


if __name__ == "__main__":
    unittest.main()
