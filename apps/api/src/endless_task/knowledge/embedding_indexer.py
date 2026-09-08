"""R5.8 EmbeddingIndexer：写时增量向量索引、全量重建与语义评分。

- 写侧：仓储钩子调用 `submit(scope, ref_id)` 入队，后台线程异步嵌入落库；
  删除调用 `remove(scope, ref_id)` 立即清理。
- 读侧：`embed_query` + `score_refs` 供混合检索做向量召回（读时暴力余弦，
  个人规模千~万级向量无需 ANN）。
- 重建：`rebuild()` 按可见性语料全量重建，并清理旧模型向量。

任何嵌入失败都只记日志降级，不阻断对话与检索主链路。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Dict, List, Mapping, Optional, Sequence, Union

from endless_task.domain.models import KnowledgeScope
from endless_task.storage.sqlite_embedding_repository import SqliteEmbeddingRepository
from endless_task.storage.sqlite_vec_search import SqliteVecSearch
from endless_task.storage.vector_math import cosine_similarity, pack_vector, unpack_vector

from .embeddings import Embedder, EmbeddingError

logger = logging.getLogger(__name__)

Scopes = Union[KnowledgeScope, str]


def _scope_value(scope: Scopes) -> str:
    return scope.value if isinstance(scope, KnowledgeScope) else str(scope)


def _distance_to_similarity(distance: float) -> float:
    """L2 距离 → 归一化点积（对单位向量：cos = 1 - d²/2），切到 [0,1]。"""
    return max(0.0, 1.0 - (distance * distance) / 2.0)


class EmbeddingIndexer:
    def __init__(
        self,
        database,
        embedder: Embedder,
        knowledge_repository,
        *,
        max_chars: int = 1500,
        batch_size: int = 8,
        clock=None,
        vec_search: Optional[SqliteVecSearch] = None,
    ) -> None:
        kwargs = {"clock": clock} if clock is not None else {}
        self._embeddings = SqliteEmbeddingRepository(database, **kwargs)
        self._vec_search = vec_search
        self._embedder = embedder
        self._knowledge_repository = knowledge_repository
        self._max_chars = max(1, int(max_chars))
        self._batch_size = max(1, int(batch_size))
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._pending: set = set()
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unavailable = False

    @property
    def model_name(self) -> str:
        return self._embedder.model_name

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="embedding-indexer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ---------- 写侧钩子 ----------

    def submit(self, scope: Scopes, ref_id: str) -> None:
        key = (_scope_value(scope), ref_id)
        with self._pending_lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self._queue.put(key)

    def remove(self, scope: Scopes, ref_id: str) -> None:
        try:
            self._embeddings.delete_ref(_scope_value(scope), ref_id)
        except Exception:  # noqa: BLE001 清理失败不影响主链路
            logger.exception("Failed to delete embeddings for %s", ref_id)

    # ---------- 后台执行 ----------

    def _run(self) -> None:
        try:
            self._embedder.ensure_ready()
        except Exception as error:  # noqa: BLE001
            self._unavailable = True
            logger.warning("Embedding backend unavailable: %s", error)
        last_retry = time.monotonic()
        while True:
            try:
                key = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set() and self._queue.empty():
                    return
                if self._stop_event.is_set():
                    continue
                continue
            scope_value, ref_id = key
            if self._unavailable and time.monotonic() - last_retry >= 30.0:
                # 网络抖动/下载中断后自愈：周期性重试后端就绪（断点续传）。
                last_retry = time.monotonic()
                try:
                    self._embedder.ensure_ready()
                    self._unavailable = False
                    logger.info("Embedding backend recovered.")
                except Exception as error:  # noqa: BLE001
                    logger.warning("Embedding backend still unavailable: %s", error)
            try:
                if not self._unavailable:
                    self._process(scope_value, ref_id)
            except Exception:  # noqa: BLE001
                logger.exception("Embedding job failed for %s/%s", scope_value, ref_id)
            finally:
                with self._pending_lock:
                    self._pending.discard(key)
            if self._stop_event.is_set() and self._queue.empty():
                return

    def _process(self, scope_value: str, ref_id: str) -> None:
        text = self.resolve_text(scope_value, ref_id)
        if text is None:
            self._embeddings.delete_ref(scope_value, ref_id)
            return
        vectors = self._embedder.embed_batch([text])
        if not vectors:
            return
        vector = vectors[0]
        self._embeddings.upsert(
            scope_value, ref_id, self._embedder.model_name,
            pack_vector(vector), len(vector),
        )

    # ---------- 文本解析（可见性即索引范围） ----------

    def resolve_text(self, scope: Scopes, ref_id: str) -> Optional[str]:
        scope_value = _scope_value(scope)
        try:
            rows = self._knowledge_repository.visible_rows(scope_value)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list visible rows for %s", scope_value)
            return None
        parts = [row["body"] or "" for row in rows if row["ref_id"] == ref_id]
        if not parts:
            return None
        return self._prepare("\n".join(parts))

    def _prepare(self, text: str) -> str:
        cleaned = (text or "").strip()
        if len(cleaned) > self._max_chars:
            cleaned = cleaned[: self._max_chars]
        return cleaned

    # ---------- 全量重建 ----------

    def rebuild(self) -> Dict[str, int]:
        self._embedder.ensure_ready()
        self._unavailable = False
        model = self._embedder.model_name
        stats: Dict[str, int] = {}
        for scope in KnowledgeScope:
            rows = self._knowledge_repository.visible_rows(scope.value)
            grouped: Dict[str, List[str]] = {}
            order: List[str] = []
            for row in rows:
                ref_id = row["ref_id"]
                if ref_id not in grouped:
                    grouped[ref_id] = []
                    order.append(ref_id)
                grouped[ref_id].append(row["body"] or "")
            items = [
                (ref_id, self._prepare("\n".join(grouped[ref_id])))
                for ref_id in order
            ]
            items = [(ref_id, text) for ref_id, text in items if text]
            indexed = 0
            for start in range(0, len(items), self._batch_size):
                batch = items[start : start + self._batch_size]
                vectors = self._embedder.embed_batch([text for _, text in batch])
                if len(vectors) != len(batch):
                    raise EmbeddingError("Embedder returned a vector count mismatch.")
                for (ref_id, _), vector in zip(batch, vectors):
                    self._embeddings.upsert(
                        scope.value, ref_id, model, pack_vector(vector), len(vector)
                    )
                indexed += len(batch)
            stats[scope.value] = indexed
        for other_model in self._embeddings.list_models():
            if other_model != model:
                self._embeddings.delete_model(other_model)
        return stats

    # ---------- 读侧：混合检索语义评分 ----------

    def embed_query(self, query: str) -> Optional[List[float]]:
        cleaned = self._prepare(query)
        if not cleaned or self._unavailable:
            return None
        try:
            self._embedder.ensure_ready()
            vectors = self._embedder.embed_batch([cleaned])
        except Exception as error:  # noqa: BLE001 查询嵌入失败 → 纯字面路径
            logger.warning("Query embedding failed: %s", error)
            return None
        return vectors[0] if vectors else None

    def score_refs(
        self,
        scope: Scopes,
        query_vector: Sequence[float],
        ref_ids: Sequence[str],
    ) -> Mapping[str, float]:
        if not ref_ids:
            return {}
        # A4/vec：sqlite-vec 可用时优先用其向量 KNN（C 层算距离），缺失则重建索引。
        if self._vec_search is not None and self._vec_search.available():
            scope_value = _scope_value(scope)
            model = self._embedder.model_name
            try:
                self._ensure_vec_index(scope_value, model, len(query_vector))
                dists = self._vec_search.scores_for_refs(
                    scope_value, model, len(query_vector),
                    pack_vector(query_vector), list(ref_ids),
                )
                if dists:
                    return {
                        ref_id: _distance_to_similarity(distance)
                        for ref_id, distance in dists.items()
                    }
            except Exception:  # noqa: BLE001 vec 失败 → 回退暴力
                logger.warning("sqlite-vec score_refs failed; falling back")
        blobs = self._embeddings.fetch_blobs(
            _scope_value(scope), list(ref_ids), self._embedder.model_name
        )
        scores: Dict[str, float] = {}
        for ref_id, blob in blobs.items():
            try:
                scores[ref_id] = cosine_similarity(query_vector, unpack_vector(blob))
            except ValueError:
                continue
        return scores

    def _ensure_vec_index(self, scope: str, model: str, dim: int) -> None:
        """幂等地把某 (scope, model) 的全部向量回填到 sqlite-vec 索引。"""
        key = (scope, model)
        if key in getattr(self, "_vec_indexed", set()):
            return
        ref_ids = self._embeddings.list_ref_ids(scope, model)
        if ref_ids:
            blobs = self._embeddings.fetch_blobs(scope, ref_ids, model)
            self._vec_search.rebuild_from_embeddings(
                scope, model, dim, [(ref_id, blobs[ref_id]) for ref_id in ref_ids if ref_id in blobs],
            )
        if not hasattr(self, "_vec_indexed"):
            self._vec_indexed = set()
        self._vec_indexed.add(key)

    # ---------- 观测 ----------

    def stats(self) -> Dict[str, object]:
        model = self._embedder.model_name
        return {
            "model": model,
            "counts": self._embeddings.count_by_scope(model),
            "models": self._embeddings.list_models(),
        }
