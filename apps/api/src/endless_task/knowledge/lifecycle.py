"""R5.10 知识生命周期：重复提案与衰减确认。

- 去重：反应式——新源落库后与存量活跃源算字符三元组 Jaccard 重叠，
  达到阈值即出 merge_source 提案（接受 = 过期新重复源、保留已有源）。
  去重是对用户刚发生动作的回应，不占主动提案预算。
- 衰减：主动式——agent 来源、长期零命中的源周期性出 expire_source
  「是否仍有效」确认提案。受全局提案预算约束；用户处理过（无论接受还是
  拒绝）后进入复核冷却期；用户亲自维护的源永不主动衰减打扰。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from endless_task.domain.models import (
    KnowledgeProposal,
    KnowledgeProposalType,
    KnowledgeProposalStatus,
    KnowledgeSource,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import RepositoryError
from endless_task.proposals.budget import ProposalBudget
from endless_task.storage.sqlite_chat_repository import SqliteChatRepository
from endless_task.storage.sqlite_knowledge_proposal_repository import (
    SqliteKnowledgeProposalRepository,
)
from endless_task.storage.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)
from endless_task.text_similarity import (
    char_trigrams as _char_trigrams,
    trigram_jaccard as _trigram_jaccard,
)
from endless_task.storage.sqlite_retrieval_event_repository import (
    SqliteRetrievalEventRepository,
)

logger = logging.getLogger(__name__)

#: 无来源会话的手动新增，锚到最近会话时使用的轮次占位符。
MANUAL_ANCHOR_TURN = "manual"


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


#: 相似度工具统一放在 text_similarity（runtime 记忆巩固也用同一套，避免环）。
char_trigrams = _char_trigrams
trigram_jaccard = _trigram_jaccard


class KnowledgeLifecycleService:
    """重复/冲突提案与衰减确认；不直接改写知识源，一切经提案确认。"""

    def __init__(
        self,
        *,
        knowledge_repository: SqliteKnowledgeRepository,
        proposal_repository: SqliteKnowledgeProposalRepository,
        retrieval_event_repository: SqliteRetrievalEventRepository,
        chat_repository: Optional[SqliteChatRepository] = None,
        budget: Optional[ProposalBudget] = None,
        duplicate_threshold: float = 0.5,
        decay_min_age_days: int = 30,
        decay_recheck_days: int = 90,
        max_decay_proposals_per_run: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not 0 < duplicate_threshold <= 1:
            raise ValueError("duplicate_threshold must be in (0, 1]")
        if decay_min_age_days < 1:
            raise ValueError("decay_min_age_days must be positive")
        if decay_recheck_days < 1:
            raise ValueError("decay_recheck_days must be positive")
        if max_decay_proposals_per_run < 1:
            raise ValueError("max_decay_proposals_per_run must be positive")
        self._knowledge_repository = knowledge_repository
        self._proposal_repository = proposal_repository
        self._retrieval_event_repository = retrieval_event_repository
        self._chat_repository = chat_repository
        self._budget = budget
        self._duplicate_threshold = duplicate_threshold
        self._decay_min_age = timedelta(days=decay_min_age_days)
        self._decay_recheck = timedelta(days=decay_recheck_days)
        self._max_decay_per_run = max_decay_proposals_per_run
        self._clock = clock

    # ---------- 去重 ----------

    def detect_duplicates(
        self, new_source: KnowledgeSource
    ) -> Optional[KnowledgeProposal]:
        """新源落库后调用：与最相似的存量活跃源超阈值则出合并提案。"""
        if new_source.status is not KnowledgeSourceStatus.ACTIVE:
            return None
        # 只比正文：标题是高度意译的短标签，参与计算会把真重复拉到阈值之下。
        new_trigrams = char_trigrams(new_source.content)
        if not new_trigrams:
            return None
        best_overlap = 0.0
        best_target: Optional[KnowledgeSource] = None
        # R5.11：同分区内比较——跨分区的相同文本是合法存在，不是重复。
        partition_sources = self._knowledge_repository.list_sources_in_partition(
            new_source.workspace_id
        )
        for existing in partition_sources:
            if existing.id == new_source.id:
                continue
            overlap = trigram_jaccard(
                new_trigrams, char_trigrams(existing.content)
            )
            if overlap >= self._duplicate_threshold and overlap > best_overlap:
                best_overlap = overlap
                best_target = existing
        if best_target is None:
            return None
        anchor = self._proposal_anchor(new_source)
        if anchor is None:
            logger.debug(
                "Duplicate detected for %s but no conversation to anchor proposal.",
                new_source.id,
            )
            return None
        conversation_id, turn_id = anchor
        payload = {
            "source_id": new_source.id,
            "target_id": best_target.id,
            "title": new_source.title,
            "target_title": best_target.title,
            "overlap": round(best_overlap, 3),
            "reason": (
                f"与已有知识《{best_target.title}》高度重复"
                f"（相似度 {int(round(best_overlap * 100))}%），建议只保留一份；"
                "确认后新加入的这条将被设为过期。"
            ),
        }
        try:
            return self._proposal_repository.create_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                proposal_type=KnowledgeProposalType.MERGE_SOURCE,
                payload=payload,
            )
        except RepositoryError as error:
            logger.warning("Duplicate proposal rejected: %s", error)
            return None

    def _proposal_anchor(
        self, source: KnowledgeSource
    ) -> Optional[tuple[str, str]]:
        """提案落点：来源会话优先，否则锚到最近活跃会话。"""
        if source.source_conversation_id and source.proposed_by_turn_id:
            return source.source_conversation_id, source.proposed_by_turn_id
        if self._chat_repository is None:
            return None
        conversations = self._chat_repository.list_conversations()
        if not conversations:
            return None
        return (
            conversations[0].id,
            source.proposed_by_turn_id or MANUAL_ANCHOR_TURN,
        )

    # ---------- 衰减确认 ----------

    def check_decay(
        self, now: Optional[datetime] = None
    ) -> tuple[KnowledgeProposal, ...]:
        """周期性扫描：agent 来源且长期零命中的源出「是否仍有效」确认提案。"""
        moment = now or self._clock()
        created: list[KnowledgeProposal] = []
        for source in self._knowledge_repository.list_sources():
            if len(created) >= self._max_decay_per_run:
                break
            if source.origin is not KnowledgeSourceOrigin.AGENT:
                continue
            created_at = _parse_timestamp(source.created_at)
            if created_at is None or moment - created_at < self._decay_min_age:
                continue
            conversation_id = source.source_conversation_id
            turn_id = source.proposed_by_turn_id
            if not conversation_id or not turn_id:
                continue
            if self._proposal_repository.find_pending_expire_by_source(
                source.id
            ) or self._proposal_repository.find_pending_merge_by_source(source.id):
                continue
            if self._recently_reviewed(source.id, moment):
                continue
            last_use = self._retrieval_event_repository.last_use_for_source(
                source.id
            )
            last_use_at = _parse_timestamp(last_use)
            if last_use_at is not None and moment - last_use_at < self._decay_min_age:
                continue
            if self._budget is not None and not self._budget.allow(
                conversation_id
            ):
                logger.debug("Decay check stopped: proposal budget exhausted.")
                break
            idle_days = (
                moment - (last_use_at or created_at)
            ).days
            payload = {
                "source_id": source.id,
                "title": source.title,
                "decay": True,
                "reason": (
                    f"这条知识已 {idle_days} 天未在对话中被引用"
                    f"（创建于 {created_at.date().isoformat()}）。"
                    "它仍然有效吗？不再需要可让它过期，仍需要直接点「不用」。"
                ),
            }
            try:
                proposal = self._proposal_repository.create_proposal(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
                    payload=payload,
                )
            except RepositoryError as error:
                logger.warning("Decay proposal rejected: %s", error)
                continue
            created.append(proposal)
        return tuple(created)

    def _recently_reviewed(self, source_id: str, moment: datetime) -> bool:
        """复核冷却：该源的过期提案在冷却期内被处理过则不再打扰。"""
        for proposal in self._proposal_repository.list_proposals_for_source(
            source_id
        ):
            if proposal.proposal_type is not KnowledgeProposalType.EXPIRE_SOURCE:
                continue
            if proposal.status not in (
                KnowledgeProposalStatus.ACCEPTED,
                KnowledgeProposalStatus.REJECTED,
            ):
                continue
            resolved_at = _parse_timestamp(proposal.resolved_at)
            if resolved_at is not None and moment - resolved_at < self._decay_recheck:
                return True
        return False
