"""B2 记忆巩固：把同类冗余记忆聚类并合并成一条更高层的记忆。

《Agentic AI Guide》第 27 章「智能体记忆系统 — 更新：冲突解决与巩固」给出：

    Consolidate(M) = Cluster(M) ∪ Summarize(Cluster(M))

本模块是其中的纯计算部分：**聚类**（字符三元组 Jaccard，确定性、无外部依赖）
与**合并**（去重 + 去子串 + 拼接，确定性 fallback）。真正的"摘要"可以由模型
完成，但那属于服务层；这里保证即使没有模型，也能给出一条稳定、可解释的合并
结果，并且**同样的记忆集合永远得到同样的签名**——服务层靠签名做到"不重复并入"。

边界：本模块不写库、不发事件、不认识 provider。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from endless_task.agent_platform import AgentPlatformError
from endless_task.text_similarity import (
    char_trigrams,
    normalize_text,
    trigram_jaccard,
)

DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_MIN_CLUSTER_SIZE = 2
DEFAULT_MAX_CLUSTER_SIZE = 5
DEFAULT_MERGE_MAX_CHARACTERS = 500

#: 提案理由前缀，服务层与前端据此识别"巩固"提案。
CONSOLIDATION_REASON_PREFIX = "巩固"


def similarity(left: str, right: str) -> float:
    """两段文本的字符三元组 Jaccard 相似度（0–1）。"""
    return trigram_jaccard(char_trigrams(left), char_trigrams(right))


def cluster_signature(records: Iterable[object]) -> str:
    """记忆集合的稳定签名（与顺序无关，用于"不重复并入"）。"""
    parts = sorted(
        f"{getattr(record, 'id', '')}:{normalize_text(str(getattr(record, 'content', '')))}"
        for record in records
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def merge_contents(
    records: Iterable[object],
    *,
    max_characters: int = DEFAULT_MERGE_MAX_CHARACTERS,
) -> str:
    """确定性合并：去重、去被包含的短句，再按原顺序拼接。"""
    if max_characters < 1:
        raise AgentPlatformError(
            "invalid_consolidation_input", "max_characters must be positive"
        )
    unique: list[str] = []
    for record in records:
        content = str(getattr(record, "content", "")).strip()
        if not content:
            continue
        if content in unique:
            continue
        # 已被更长的内容包含 → 丢弃（避免"用户偏好周五发布"压过它的详细版本）。
        if any(content != other and content in other for other in unique):
            continue
        unique = [other for other in unique if not (other != content and other in content)]
        unique.append(content)
    if not unique:
        return ""
    merged = "；".join(unique)
    if len(merged) > max_characters:
        merged = merged[:max_characters] + "…"
    return merged


@dataclass(frozen=True)
class MemoryCluster:
    """一组可合并的同类记忆。"""

    kind: str
    records: tuple[object, ...]
    signature: str
    merged_content: str
    representative_conversation_id: str
    representative_turn_id: str

    @property
    def memory_ids(self) -> tuple[str, ...]:
        return tuple(str(getattr(record, "id", "")) for record in self.records)

    @property
    def contents(self) -> tuple[str, ...]:
        return tuple(str(getattr(record, "content", "")) for record in self.records)

    @property
    def size(self) -> int:
        return len(self.records)

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "memoryIds": list(self.memory_ids),
            "contents": list(self.contents),
            "signature": self.signature,
            "mergedContent": self.merged_content,
            "conversationId": self.representative_conversation_id,
            "turnId": self.representative_turn_id,
        }


def cluster_memories(
    records: Sequence[object],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE,
) -> tuple[MemoryCluster, ...]:
    """按 kind 分组后做确定性贪心聚类（与 seed 相似度达阈值即入簇）。"""
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise AgentPlatformError(
            "invalid_consolidation_input", "threshold must be a number"
        )
    if not 0 <= float(threshold) <= 1:
        raise AgentPlatformError(
            "invalid_consolidation_input", "threshold must be within [0, 1]"
        )
    if min_cluster_size < 2:
        raise AgentPlatformError(
            "invalid_consolidation_input", "min_cluster_size must be >= 2"
        )
    if max_cluster_size < min_cluster_size:
        raise AgentPlatformError(
            "invalid_consolidation_input",
            "max_cluster_size must be >= min_cluster_size",
        )
    by_kind: dict[str, list[object]] = {}
    for record in records:
        raw_kind = getattr(record, "kind", "fact")
        kind = str(getattr(raw_kind, "value", raw_kind))
        by_kind.setdefault(kind, []).append(record)

    clusters: list[MemoryCluster] = []
    for kind in sorted(by_kind):
        # 固定顺序：按 id 排序，保证同样输入永远得到同样聚类。
        pending = sorted(
            by_kind[kind], key=lambda item: str(getattr(item, "id", ""))
        )
        while pending:
            seed = pending.pop(0)
            seed_content = str(getattr(seed, "content", ""))
            members = [seed]
            rest: list[object] = []
            for candidate in pending:
                if len(members) >= max_cluster_size:
                    rest.append(candidate)
                    continue
                if (
                    similarity(
                        seed_content, str(getattr(candidate, "content", ""))
                    )
                    >= float(threshold)
                ):
                    members.append(candidate)
                else:
                    rest.append(candidate)
            pending = rest
            if len(members) < min_cluster_size:
                continue
            clusters.append(
                MemoryCluster(
                    kind=kind,
                    records=tuple(members),
                    signature=cluster_signature(members),
                    merged_content=merge_contents(members),
                    representative_conversation_id=str(
                        getattr(seed, "source_conversation_id", "")
                    ),
                    representative_turn_id=str(
                        getattr(seed, "source_turn_id", "")
                    ),
                )
            )
    # 大簇优先，其次按签名，输出稳定。
    clusters.sort(key=lambda item: (-item.size, item.signature))
    return tuple(clusters)


__all__ = [
    "CONSOLIDATION_REASON_PREFIX",
    "DEFAULT_MAX_CLUSTER_SIZE",
    "DEFAULT_MERGE_MAX_CHARACTERS",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "MemoryCluster",
    "cluster_memories",
    "cluster_signature",
    "merge_contents",
    "similarity",
]
