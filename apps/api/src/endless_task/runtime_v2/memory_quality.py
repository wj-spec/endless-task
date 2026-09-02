from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from endless_task.knowledge.embeddings import Embedder, EmbeddingError

if TYPE_CHECKING:
    from endless_task.runtime_v2.domain import RuntimeV2MemoryRecord
    from endless_task.storage.sqlite_runtime_v2_memory_repository import (
        SqliteRuntimeV2MemoryRepository,
    )


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimilarityResult:
    """与候选记忆的相似度结果。"""

    record: "RuntimeV2MemoryRecord"
    similarity: float


class RuntimeV2MemoryQualityService:
    """v2 记忆质量闭环：写入侧语义去重 + 更新候选（mem0 的 NOOP/UPDATE 语义）。

    自动提取/镜像写入前调用 ``find_similar``：

    1. 字面精确匹配（快路径，优先命中，similarity=1.0）；
    2. embedder 可用时，对候选 active 记忆与新内容计算余弦相似度；
    3. embedder 不可用或推理失败时静默降级为字面去重，不阻断主链路。

    调用方按相似度决策：

    - ``similarity >= similarity_threshold``（默认 0.85）：重复 → 跳过新增（NOOP）；
    - ``update_threshold <= similarity < similarity_threshold`` 且内容不同：
      同主题更强/更新的内容 → 执行 UPDATE（写入新记忆并取代旧记忆）；
    - 其余：新增（ADD）。

    去重范围为同 conversation 的未过期、未被取代的 active 记忆。
    """

    def __init__(
        self,
        *,
        memory_repository: "SqliteRuntimeV2MemoryRepository",
        embedder: Optional[Embedder] = None,
        similarity_threshold: float = 0.85,
        update_threshold: float = 0.6,
        max_candidates: int = 200,
    ) -> None:
        if not 0 < similarity_threshold < 1:
            raise ValueError("similarity_threshold must be within (0, 1)")
        if not 0 < update_threshold <= similarity_threshold:
            raise ValueError("update_threshold must be within (0, similarity_threshold]")
        self._memory_repository = memory_repository
        self._embedder = embedder
        self._similarity_threshold = similarity_threshold
        self._update_threshold = update_threshold
        self._max_candidates = max_candidates

    @property
    def similarity_threshold(self) -> float:
        return self._similarity_threshold

    @property
    def update_threshold(self) -> float:
        return self._update_threshold

    def find_duplicate(
        self,
        *,
        conversation_id: str,
        content: str,
        exclude_id: Optional[str] = None,
    ) -> Optional["RuntimeV2MemoryRecord"]:
        """重复检测（NOOP 判定）：相似度达到重复阈值即返回相似记忆。"""
        result = self.find_similar(
            conversation_id=conversation_id,
            content=content,
            exclude_id=exclude_id,
        )
        if result is None or result.similarity < self._similarity_threshold:
            return None
        return result.record

    def find_update_candidate(
        self,
        *,
        conversation_id: str,
        content: str,
        exclude_id: Optional[str] = None,
    ) -> Optional["RuntimeV2MemoryRecord"]:
        """更新候选（UPDATE 判定）：相似度在更新区间且内容不同。"""
        result = self.find_similar(
            conversation_id=conversation_id,
            content=content,
            exclude_id=exclude_id,
        )
        if result is None:
            return None
        if result.similarity >= self._similarity_threshold:
            return None
        if result.similarity < self._update_threshold:
            return None
        if result.record.content.strip() == content.strip():
            return None
        return result.record

    def find_similar(
        self,
        *,
        conversation_id: str,
        content: str,
        exclude_id: Optional[str] = None,
    ) -> Optional[SimilarityResult]:
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
                return SimilarityResult(
                    record=self._memory_repository.get_memory(memory_id),
                    similarity=1.0,
                )

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
        best: Optional[SimilarityResult] = None
        for (memory_id, _), vector in zip(candidates, vectors[1:]):
            if memory_id == exclude_id:
                continue
            similarity = _cosine_similarity(target, vector)
            if similarity is None:
                continue
            if best is None or similarity > best.similarity:
                best = SimilarityResult(
                    record=self._memory_repository.get_memory(memory_id),
                    similarity=similarity,
                )
        return best


def _cosine_similarity(left: list[float], right: list[float]) -> Optional[float]:
    """归一化向量的点积即余弦相似度；维度不一致返回 None。"""
    if not left or len(left) != len(right):
        return None
    return sum(a * b for a, b in zip(left, right))
