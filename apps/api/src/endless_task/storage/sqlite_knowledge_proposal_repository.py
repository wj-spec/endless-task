"""P5 R5.5 知识提案仓储：add_source / expire_source 提案的创建与确认流。"""

from __future__ import annotations

import json
from typing import Callable, Optional, Sequence

from endless_task.domain.models import (
    KnowledgeProposal,
    KnowledgeProposalStatus,
    KnowledgeProposalType,
    KnowledgeSource,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def knowledge_source_from_row(row) -> KnowledgeSource:
    return KnowledgeSource(
        id=row["id"],
        kind=KnowledgeSourceKind(row["kind"]),
        origin=KnowledgeSourceOrigin(row["origin"]),
        title=row["title"],
        content=row["content"],
        status=KnowledgeSourceStatus(row["status"]),
        file_name=row["file_name"],
        source_conversation_id=row["source_conversation_id"],
        proposed_by_turn_id=row["proposed_by_turn_id"],
        user_edited_at=row["user_edited_at"],
        expires_at=row["expires_at"],
        expired_at=row["expired_at"],
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        file_size=row["file_size"],
        file_sha256=row["file_sha256"],
    )


class SqliteKnowledgeProposalRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 120,
        max_content_chars: int = 4000,
        max_reason_chars: int = 200,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_content_chars = max_content_chars
        self._max_reason_chars = max_reason_chars
        self._clock = clock
        self._id_factory = id_factory

    # ---------- 创建与读取 ----------

    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        proposal_type: KnowledgeProposalType,
        payload: dict,
    ) -> KnowledgeProposal:
        if not conversation_id.strip() or not turn_id.strip():
            raise ValidationError("Proposal source conversation and turn are required.")
        normalized_payload = self._validate_payload(proposal_type, payload)

        existing: Optional[KnowledgeProposal] = None
        if proposal_type is KnowledgeProposalType.ADD_SOURCE:
            existing = self.find_pending_add_by_content(
                str(normalized_payload["content"])
            )
        else:
            existing = self.find_pending_expire_by_source(
                str(normalized_payload["source_id"])
            )
        if existing is not None:
            return existing

        now = self._clock()
        proposal_id = self._id_factory("knp")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_proposals (
                    id, proposal_type, payload, status,
                    conversation_id, turn_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    proposal_type.value,
                    json.dumps(normalized_payload, ensure_ascii=False),
                    KnowledgeProposalStatus.PENDING.value,
                    conversation_id.strip(),
                    turn_id.strip(),
                    now,
                    now,
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> KnowledgeProposal:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Knowledge proposal not found: {proposal_id}")
        return self._from_row(row)

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[KnowledgeProposal]:
        query = "SELECT * FROM knowledge_proposals WHERE conversation_id = ?"
        params: list = [conversation_id]
        if not include_resolved:
            query += " AND status = ?"
            params.append(KnowledgeProposalStatus.PENDING.value)
        query += " ORDER BY created_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_pending(self, *, limit: int) -> Sequence[KnowledgeProposal]:
        query = (
            "SELECT * FROM knowledge_proposals WHERE status = ? "
            "ORDER BY created_at, id LIMIT ?"
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                query, (KnowledgeProposalStatus.PENDING.value, limit)
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_pending_add_by_content(self, content: str) -> Optional[KnowledgeProposal]:
        normalized = content.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_proposals "
                "WHERE status = ? AND proposal_type = ? ORDER BY created_at, id",
                (
                    KnowledgeProposalStatus.PENDING.value,
                    KnowledgeProposalType.ADD_SOURCE.value,
                ),
            ).fetchall()
        for row in rows:
            proposal = self._from_row(row)
            if str(proposal.payload.get("content", "")).strip() == normalized:
                return proposal
        return None

    def find_pending_expire_by_source(
        self, source_id: str
    ) -> Optional[KnowledgeProposal]:
        normalized = source_id.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_proposals "
                "WHERE status = ? AND proposal_type = ? ORDER BY created_at, id",
                (
                    KnowledgeProposalStatus.PENDING.value,
                    KnowledgeProposalType.EXPIRE_SOURCE.value,
                ),
            ).fetchall()
        for row in rows:
            proposal = self._from_row(row)
            if str(proposal.payload.get("source_id", "")).strip() == normalized:
                return proposal
        return None

    # ---------- 确认流 ----------

    def accept_proposal(
        self, proposal_id: str
    ) -> tuple[KnowledgeProposal, KnowledgeSource]:
        now = self._clock()
        source_id = self._id_factory("ks")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Knowledge proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not KnowledgeProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
                connection.execute(
                    """
                    INSERT INTO knowledge_sources (
                        id, kind, origin, title, content, file_name, status,
                        source_conversation_id, proposed_by_turn_id,
                        expires_at, created_at, updated_at
                    )
                    VALUES (?, 'note', 'agent', ?, ?, NULL, 'active', ?, ?, NULL, ?, ?)
                    """,
                    (
                        source_id,
                        str(proposal.payload["title"]),
                        str(proposal.payload["content"]),
                        proposal.conversation_id,
                        proposal.turn_id,
                        now,
                        now,
                    ),
                )
            else:
                target_id = str(proposal.payload["source_id"])
                source_row = connection.execute(
                    "SELECT * FROM knowledge_sources WHERE id = ?", (target_id,)
                ).fetchone()
                if source_row is None:
                    raise NotFoundError(f"Knowledge source not found: {target_id}")
                if source_row["status"] != KnowledgeSourceStatus.ACTIVE.value:
                    raise InvalidStateError(
                        "Only active knowledge sources can expire."
                    )
                connection.execute(
                    "UPDATE knowledge_sources SET status = 'expired', "
                    "expired_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, target_id),
                )
                source_id = target_id
            connection.execute(
                """
                UPDATE knowledge_proposals
                SET status = ?, resolved_source_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    KnowledgeProposalStatus.ACCEPTED.value,
                    source_id,
                    now,
                    now,
                    proposal_id,
                ),
            )
            source_row = connection.execute(
                "SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)
            ).fetchone()
        return self.get_proposal(proposal_id), knowledge_source_from_row(source_row)

    def reject_proposal(self, proposal_id: str) -> KnowledgeProposal:
        return self._resolve(proposal_id, KnowledgeProposalStatus.REJECTED)

    def cancel_proposal(self, proposal_id: str) -> KnowledgeProposal:
        return self._resolve(proposal_id, KnowledgeProposalStatus.CANCELLED)

    def cancel_proposals_for_conversation(self, conversation_id: str) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE conversation_id = ? AND status = ?
                """,
                (
                    KnowledgeProposalStatus.CANCELLED.value,
                    now,
                    now,
                    conversation_id.strip(),
                    KnowledgeProposalStatus.PENDING.value,
                ),
            )
        return cursor.rowcount or 0

    # ---------- 内部 ----------

    def _resolve(
        self, proposal_id: str, status: KnowledgeProposalStatus
    ) -> KnowledgeProposal:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Knowledge proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not KnowledgeProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            connection.execute(
                """
                UPDATE knowledge_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, now, now, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def _validate_payload(
        self, proposal_type: KnowledgeProposalType, payload: dict
    ) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("Knowledge proposal payload must be an object.")
        reason = str(payload.get("reason", "")).strip() or "助手建议"
        if len(reason) > self._max_reason_chars:
            reason = reason[: self._max_reason_chars]
        if proposal_type is KnowledgeProposalType.ADD_SOURCE:
            title = str(payload.get("title", "")).strip()
            content = str(payload.get("content", "")).strip()
            if not title or not content:
                raise ValidationError("add_source proposals require title and content.")
            if len(title) > self._max_title_chars:
                title = title[: self._max_title_chars]
            if len(content) > self._max_content_chars:
                content = content[: self._max_content_chars]
            return {"title": title, "kind": "note", "content": content, "reason": reason}
        source_id = str(payload.get("source_id", "")).strip()
        if not source_id:
            raise ValidationError("expire_source proposals require source_id.")
        normalized = {"source_id": source_id, "reason": reason}
        title = str(payload.get("title", "")).strip()
        if title:
            normalized["title"] = title
        return normalized

    @staticmethod
    def _from_row(row) -> KnowledgeProposal:
        return KnowledgeProposal(
            id=row["id"],
            proposal_type=KnowledgeProposalType(row["proposal_type"]),
            payload=json.loads(row["payload"]),
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            status=KnowledgeProposalStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_source_id=row["resolved_source_id"],
            resolved_at=row["resolved_at"],
        )
