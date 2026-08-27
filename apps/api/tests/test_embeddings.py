"""R5.8 向量工具、向量仓储与嵌入后端单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.knowledge.embeddings import (
    EmbeddingError,
    LocalOnnxEmbedder,
    ProviderEmbedder,
    build_embedder,
    cls_pool_and_normalize,
)
from endless_task.storage import Database, SqliteEmbeddingRepository
from endless_task.storage.vector_math import (
    cosine_similarity,
    pack_vector,
    unpack_vector,
)


class VectorMathTest(unittest.TestCase):
    def test_pack_unpack_roundtrip(self) -> None:
        vector = [0.0, 1.5, -2.25, 3.5]
        self.assertEqual(unpack_vector(pack_vector(vector)), vector)

    def test_pack_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            pack_vector([])

    def test_unpack_invalid_blob_raises(self) -> None:
        with self.assertRaises(ValueError):
            unpack_vector(b"")
        with self.assertRaises(ValueError):
            unpack_vector(b"abc")

    def test_cosine_similarity(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1, 1], [-1, -1]), -1.0)
        self.assertEqual(cosine_similarity([1, 0], [0]), 0.0)
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_cls_pool_and_normalize(self) -> None:
        hidden = [
            [[3.0, 4.0], [9.0, 9.0]],
            [[0.0, 0.0], [1.0, 1.0]],
        ]
        vectors = cls_pool_and_normalize(hidden)
        self.assertEqual(len(vectors), 2)
        self.assertAlmostEqual(vectors[0][0], 0.6)
        self.assertAlmostEqual(vectors[0][1], 0.8)
        self.assertEqual(vectors[1], [0.0, 0.0])


class SqliteEmbeddingRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "embeddings.db"
        )
        self.database.initialize()
        self.repository = SqliteEmbeddingRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_upsert_get_and_overwrite(self) -> None:
        blob = pack_vector([1.0, 2.0])
        self.repository.upsert("source", "ks_1", "model-a", blob, 2)
        self.assertEqual(self.repository.get_blob("source", "ks_1", "model-a"), blob)
        overwritten = pack_vector([3.0, 4.0])
        self.repository.upsert("source", "ks_1", "model-a", overwritten, 2)
        self.assertEqual(
            self.repository.get_blob("source", "ks_1", "model-a"), overwritten
        )

    def test_fetch_blobs_filters_scope_and_model(self) -> None:
        self.repository.upsert("source", "ks_1", "model-a", pack_vector([1.0]), 1)
        self.repository.upsert("source", "ks_2", "model-b", pack_vector([2.0]), 1)
        self.repository.upsert("memory", "mem_1", "model-a", pack_vector([3.0]), 1)
        fetched = self.repository.fetch_blobs("source", ["ks_1", "ks_2", "mem_1"], "model-a")
        self.assertEqual(set(fetched), {"ks_1"})
        self.assertEqual(self.repository.fetch_blobs("source", [], "model-a"), {})

    def test_delete_ref_and_model(self) -> None:
        self.repository.upsert("source", "ks_1", "model-a", pack_vector([1.0]), 1)
        self.repository.upsert("source", "ks_1", "model-b", pack_vector([2.0]), 1)
        self.assertEqual(self.repository.delete_ref("source", "ks_1"), 2)
        self.repository.upsert("memory", "mem_1", "model-b", pack_vector([3.0]), 1)
        self.assertEqual(self.repository.delete_model("model-b"), 1)

    def test_count_by_scope_and_models(self) -> None:
        self.repository.upsert("source", "ks_1", "model-a", pack_vector([1.0]), 1)
        self.repository.upsert("source", "ks_2", "model-a", pack_vector([2.0]), 1)
        self.repository.upsert("memory", "mem_1", "model-b", pack_vector([3.0]), 1)
        self.assertEqual(
            self.repository.count_by_scope("model-a"), {"source": 2}
        )
        self.assertEqual(
            self.repository.count_by_scope(), {"source": 2, "memory": 1}
        )
        self.assertEqual(self.repository.list_models(), ["model-a", "model-b"])


class FakeEmbeddingItem:
    def __init__(self, index: int, embedding) -> None:
        self.index = index
        self.embedding = embedding


class FakeEmbeddingResponse:
    def __init__(self, data) -> None:
        self.data = data


class FakeEmbeddingsEndpoint:
    def __init__(self, error=None, responder=None) -> None:
        self.error = error
        self.responder = responder
        self.calls: list = []

    def create(self, *, model: str, input):  # noqa: A002 对齐 openai SDK 签名
        self.calls.append((model, list(input)))
        if self.error is not None:
            raise self.error
        if self.responder is not None:
            return self.responder(list(input))
        return FakeEmbeddingResponse(
            [FakeEmbeddingItem(i, [float(i), 1.0]) for i in range(len(input))]
        )


class FakeOpenAIClient:
    def __init__(self, endpoint) -> None:
        self.embeddings = endpoint


class ProviderEmbedderTest(unittest.TestCase):
    def test_embed_batch_orders_by_index(self) -> None:
        endpoint = FakeEmbeddingsEndpoint()
        embedder = ProviderEmbedder(
            model="emb-1", api_key="k", client=FakeOpenAIClient(endpoint)
        )
        vectors = embedder.embed_batch(["a", "b"])
        self.assertEqual(vectors, [[0.0, 1.0], [1.0, 1.0]])
        self.assertEqual(endpoint.calls, [("emb-1", ["a", "b"])])

    def test_empty_text_replaced_with_space(self) -> None:
        endpoint = FakeEmbeddingsEndpoint()
        embedder = ProviderEmbedder(
            model="emb-1", api_key="k", client=FakeOpenAIClient(endpoint)
        )
        embedder.embed_batch(["", "  "])
        self.assertEqual(endpoint.calls[0][1], [" ", " "])

    def test_error_raises_embedding_error(self) -> None:
        endpoint = FakeEmbeddingsEndpoint(error=RuntimeError("boom"))
        embedder = ProviderEmbedder(
            model="emb-1", api_key="k", client=FakeOpenAIClient(endpoint)
        )
        with self.assertRaises(EmbeddingError):
            embedder.embed_batch(["a"])

    def test_vector_count_mismatch_raises(self) -> None:
        def responder(inputs):
            return FakeEmbeddingResponse([FakeEmbeddingItem(0, [1.0])])

        endpoint = FakeEmbeddingsEndpoint(responder=responder)
        embedder = ProviderEmbedder(
            model="emb-1", api_key="k", client=FakeOpenAIClient(endpoint)
        )
        with self.assertRaises(EmbeddingError):
            embedder.embed_batch(["a", "b"])


class FakeEncoding:
    def __init__(self, ids) -> None:
        self.ids = ids


class FakeTokenizer:
    def encode(self, text: str) -> FakeEncoding:  # noqa: ARG002
        return FakeEncoding([7, 8, 9])


class FakeTensor:
    def __init__(self, data) -> None:
        self._data = data

    def tolist(self):
        return self._data


class FakeOnnxSession:
    def __init__(self) -> None:
        self.calls: list = []

    def run(self, output_names, feed):  # noqa: ARG002
        self.calls.append(feed)
        batch = len(feed["input_ids"])
        hidden = [[[3.0, 4.0], [9.0, 9.0]] for _ in range(batch)]
        return [FakeTensor(hidden)]


def _make_local_embedder(cache_dir: Path, session: FakeOnnxSession) -> LocalOnnxEmbedder:
    return LocalOnnxEmbedder(
        cache_dir,
        session_factory=lambda: (
            session,
            ("input_ids", "attention_mask", "token_type_ids"),
            "last_hidden_state",
        ),
        tokenizer_factory=lambda: FakeTokenizer(),
    )


class LocalOnnxEmbedderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_embed_batch_pools_cls_and_normalizes(self) -> None:
        session = FakeOnnxSession()
        embedder = _make_local_embedder(self.cache_dir, session)
        vectors = embedder.embed_batch(["你好", "世界"])
        self.assertEqual(vectors, [[0.6, 0.8], [0.6, 0.8]])
        self.assertEqual(len(session.calls), 1)
        feed = session.calls[0]
        self.assertEqual(feed["input_ids"].tolist(), [[7, 8, 9], [7, 8, 9]])

    def test_ensure_ready_is_idempotent(self) -> None:
        session = FakeOnnxSession()
        embedder = _make_local_embedder(self.cache_dir, session)
        embedder.ensure_ready()
        embedder.ensure_ready()
        embedder.embed_batch(["a"])
        self.assertEqual(len(session.calls), 1)

    def test_download_failure_raises_embedding_error(self) -> None:
        embedder = LocalOnnxEmbedder(
            self.cache_dir,
            repo_id="endless-task-test/missing",
            url_base="http://127.0.0.1:9",
            download_attempts=1,
        )
        with self.assertRaises(EmbeddingError):
            embedder.ensure_ready()


class BuildEmbedderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _build(self, **overrides):
        kwargs = dict(
            backend="local",
            embedding_model=None,
            provider_name="deepseek",
            api_key="key",
            base_url=None,
            cache_dir=self.cache_dir,
        )
        kwargs.update(overrides)
        return build_embedder(**kwargs)

    def test_local_backend(self) -> None:
        embedder = self._build(backend="local")
        self.assertIsInstance(embedder, LocalOnnxEmbedder)
        self.assertEqual(embedder.model_name, "bge-small-zh-v1.5")

    def test_provider_backend_requires_model_and_key(self) -> None:
        self.assertIsNone(self._build(backend="provider"))
        self.assertIsNone(
            self._build(backend="provider", embedding_model="emb", api_key=None)
        )

    def test_provider_backend(self) -> None:
        embedder = self._build(backend="provider", embedding_model="emb")
        self.assertIsInstance(embedder, ProviderEmbedder)
        self.assertEqual(embedder.model_name, "emb")

    def test_openai_compatible_requires_base_url(self) -> None:
        self.assertIsNone(
            self._build(
                backend="provider",
                embedding_model="emb",
                provider_name="openai-compatible",
            )
        )
        embedder = self._build(
            backend="provider",
            embedding_model="emb",
            provider_name="openai-compatible",
            base_url="http://127.0.0.1:9000/v1",
        )
        self.assertIsInstance(embedder, ProviderEmbedder)

    def test_unknown_backend_returns_none(self) -> None:
        self.assertIsNone(self._build(backend="bogus"))


if __name__ == "__main__":
    unittest.main()
