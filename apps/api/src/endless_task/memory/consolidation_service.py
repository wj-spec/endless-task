"""B2 记忆巩固服务：聚类 → 提案 → 确认后并入。

流程刻意与知识去重/衰减保持一致（**一切经提案确认，不直接改写记忆**）：

1. ``plan()``：读活跃记忆，按 kind + 文本相似度聚类（钉住的记忆不参与）；
2. ``create_proposals()``：为每个聚类生成一条记忆提案（内容 = 合并后的高层记忆），
   同时写入 ``memory_consolidations`` 记录溯源与签名（UNIQUE 保证不重复并入）；
3. 用户在提案卡上确认后，``finalize()`` 把原记忆标记为"已并入"
   （``expired_reason=consolidated`` + ``superseded_by=洞察记忆``），原记忆仍可溯源。

服务不认识 provider：摘要由纯函数的确定性合并给出（``merge_contents``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from endless_task.domain.models import MemoryProposal, MemoryRecord
from endless_task.runtime.memory_consolidation import (
    CONSOLIDATION_REASON_PREFIX,
    DEFAULT_MAX_CLUSTER_SIZE,
    DEFAULT_MIN_CLUSTER_SIZE,
    DEFAULT_SIMILARITY_THRESHOLD,
    MemoryCluster,
    cluster_memories,
)
from endless_task.storage import (
    SqliteMemoryConsolidationRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)

CONSOLIDATED_REASON = "consolidated"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ConsolidationCandidate:
    """一个待确认的巩固候选（聚类 + 生成的提案）。"""

    cluster: MemoryCluster
    proposal: MemoryProposal
    importance: float

    def as_json(self) -> dict[str, object]:
        return {
            **self.cluster.as_json(),
            "proposalId": self.proposal.id,
            "importance": self.importance,
            "reason": self.proposal.reason,
        }


@dataclass(frozen=True)
class ConsolidationRunReport:
    clusters: tuple[MemoryCluster, ...] = ()
    created: tuple[ConsolidationCandidate, ...] = ()
    skipped_signatures: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "clusterCount": len(self.clusters),
            "createdCount": len(self.created),
            "created": [item.as_json() for item in self.created],
            "skipped": list(self.skipped_signatures),
        }


class MemoryConsolidationService:
    def __init__(
        self,
        *,
        memory_repository: SqliteMemoryRepository,
        proposal_repository: SqliteMemoryProposalRepository,
        consolidation_repository: SqliteMemoryConsolidationRepository,
        clock: Callable[[], datetime] = _utc_now,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
        max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE,
        max_clusters_per_run: int = 3,
    ) -> None:
        if max_clusters_per_run <= 0:
            raise ValueError("max_clusters_per_run must be positive")
        self._memory_repository = memory_repository
        self._proposal_repository = proposal_repository
        self._consolidation_repository = consolidation_repository
        self._clock = clock
        self._threshold = threshold
        self._min_cluster_size = min_cluster_size
        self._max_cluster_size = max_cluster_size
        self._max_clusters_per_run = max_clusters_per_run

    # ---------- 规划 ----------

    def plan(self) -> tuple[MemoryCluster, ...]:
        """当前可巩固的聚类（排除钉住的记忆与已并入过的集合）。"""
        memories = [
            memory
            for memory in self._memory_repository.list_memories()
            if not memory.pinned
        ]
        clusters = cluster_memories(
            memories,
            threshold=self._threshold,
            min_cluster_size=self._min_cluster_size,
            max_cluster_size=self._max_cluster_size,
        )
        return tuple(
            cluster
            for cluster in clusters
            if self._consolidation_repository.find_by_signature(cluster.signature)
            is None
        )

    # ---------- 提案 ----------

    def create_proposals(self) -> ConsolidationRunReport:
        """为每个可巩固聚类生成一条记忆提案（走既有确认流）。"""
        clusters = self.plan()
        created: list[ConsolidationCandidate] = []
        skipped: list[str] = []
        for cluster in clusters:
            if len(created) >= self._max_clusters_per_run:
                break
            if not cluster.merged_content.strip():
                skipped.append(cluster.signature)
                continue
            existing = self._consolidation_repository.find_by_signature(
                cluster.signature
            )
            if existing is not None:
                skipped.append(cluster.signature)
                continue
            proposal = self._proposal_repository.create_proposal(
                conversation_id=cluster.representative_conversation_id,
                turn_id=cluster.representative_turn_id or "consolidation",
                kind=cluster.kind,
                content=cluster.merged_content,
                reason=(
                    f"{CONSOLIDATION_REASON_PREFIX}：把 {cluster.size} 条同类记忆"
                    "合并成一条高层记忆"
                ),
            )
            self._consolidation_repository.create(
                proposal_id=proposal.id,
                signature=cluster.signature,
                kind=cluster.kind,
                source_memory_ids=cluster.memory_ids,
            )
            created.append(
                ConsolidationCandidate(
                    cluster=cluster,
                    proposal=proposal,
                    importance=max(
                        (
                            float(getattr(record, "importance", 0.5))
                            for record in cluster.records
                        ),
                        default=0.5,
                    ),
                )
            )
        return ConsolidationRunReport(
            clusters=clusters,
            created=tuple(created),
            skipped_signatures=tuple(skipped),
        )

    # ---------- 确认后落库 ----------

    def finalize(
        self,
        proposal_id: str,
        insight_memory: MemoryRecord,
    ) -> Optional[str]:
        """把聚类里的原记忆标记为"已并入"，并登记洞察记忆 id。

        返回被并入的原记忆 id 列表（无对应巩固记录时返回 None）。
        """
        record = self._consolidation_repository.find_by_proposal(proposal_id)
        if record is None:
            return None
        if record.status == "accepted":
            return ()
        # 洞察记忆继承来源里最高的重要性，避免合并后"变轻"被 B3 忘掉。
        source_importances = [
            self._safe_importance(memory_id)
            for memory_id in record.source_memory_ids
        ]
        top_importance = max(source_importances, default=0.5)
        if top_importance > insight_memory.importance:
            self._memory_repository.set_memory_importance(
                insight_memory.id, top_importance
            )
        merged: list[str] = []
        for memory_id in record.source_memory_ids:
            if memory_id == insight_memory.id:
                continue
            try:
                self._memory_repository.expire_memory(
                    memory_id,
                    reason=CONSOLIDATED_REASON,
                    superseded_by=insight_memory.id,
                )
            except Exception:  # noqa: BLE001 已过期/已删除的源记忆跳过
                continue
            merged.append(memory_id)
        self._consolidation_repository.mark_resolved(
            record.id,
            status="accepted",
            insight_memory_id=insight_memory.id,
        )
        return tuple(merged)

    def reject(self, proposal_id: str) -> Optional[str]:
        """提案被拒绝：登记 rejected，签名不再重复出提案。"""
        record = self._consolidation_repository.find_by_proposal(proposal_id)
        if record is None:
            return None
        if record.status == "pending":
            self._consolidation_repository.mark_resolved(
                record.id, status="rejected"
            )
        return record.id

    def list_records(self, *, include_resolved: bool = True):
        return self._consolidation_repository.list_records(
            include_resolved=include_resolved
        )

    def _safe_importance(self, memory_id: str) -> float:
        try:
            return float(self._memory_repository.get_memory(memory_id).importance)
        except Exception:  # noqa: BLE001 源记忆可能已被删除
            return 0.5


__all__ = [
    "CONSOLIDATED_REASON",
    "ConsolidationCandidate",
    "ConsolidationRunReport",
    "MemoryConsolidationService",
]
