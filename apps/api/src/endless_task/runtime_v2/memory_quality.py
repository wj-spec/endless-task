from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from endless_task.knowledge.embeddings import Embedder, EmbeddingError

if TYPE_CHECKING:
    from endless_task.runtime_v2.domain import RuntimeV2MemoryRecord
    from endless_task.storage.sqlite_runtime_v2_memory_repository import (
        SqliteRuntimeV2MemoryRepository,
    )


logger = logging.getLogger(__name__)


class RuntimeV2MemoryQualityService:
    """v2 记忆质量闭环：写入侧语义去重（mem0 的 NOOP 语义）。

    自动提取/镜像写入前调用 ``find_duplicate``：

    1. 字面精确匹配（快路径，优先命中）；
    2. embedder 可用时，对候选 active 记忆与新内容计算余弦相似度，
       达到阈值即视为重复（跳过新增，不污染长期记忆）；
    3. embedder 不可用或推理失败时静默降级为字面去重，不阻断主链路。

    去重范围为同 conversation 的未过期 active 记忆（v2 记忆均带
    conversation_id 来源）。
    """

    def __init__(
        self,
        *,
        memory_repository: "SqliteRuntimeV2MemoryRepository",
        embedder: Optional[Embedder] = None,
        similarity_threshold: float = 0.85,
        max_candidates: int = 200,
    ) -> None:
        if not 0 < similarity_threshold < 1:
            raise ValueError("similarity_threshold must be within (0, 1)")
        self._memory_repository = memory_repository
        self._embedder = embedder
        self._similarity_threshold = similarity_threshold
        self._max_candidates = max_candidates

    def find_duplicate(
        self,
        *,
        conversation_id: str,
        content: str,
        exclude_id: Optional[str] = None,
    ) -> Optional["RuntimeV2MemoryRecord"]:
        normalized = content.strip()
        if not normalized:
            return None
        candidates = self._memory_repository.list_active_memories_content(
            conversation_id,
            limit=self._max_candidates,
        )
        if not candidates:
            return None

        # 快路径:字面精确匹配。
        for memory_id, candidate_content in candidates:
            if memory_id == exclude_id:
                continue
            if candidate_content.strip() == normalized:
                return self._memory_repository.get_memory(memory_id)

        if self._embedder is None:
            return None
        try:
            self._embedder.ensure_ready()
            texts = [normalized] + [candidate for _, candidate in candidates]
            vectors = self._embedder.embed_batch(texts)
        except EmbeddingError as error:
            logger.debug("Semantic memory dedup unavailable: %s", error)
            return None
        if not vectors or len(vectors) != len(texts):
            return None
        target = vectors[0]
        for (memory_id, _), vector in zip(candidates, vectors[1:]):
            if memory_id == exclude_id:
                continue
            similarity = _cosine_similarity(target, vector)
            if similarity is not None and similarity >= self._similarity_threshold:
                return self._memory_repository.get_memory(memory_id)
        return None


def _cosine_similarity(left: list[float], right: list[float]) -> Optional[float]:
    """归一化向量的点积即余弦相似度；维度不一致返回 None。"""
    if not left or len(left) != len(right):
        return None
    return sum(a * b for a, b in zip(left, right))
