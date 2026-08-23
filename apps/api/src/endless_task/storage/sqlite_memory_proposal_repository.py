from __future__ import annotations

from typing import Callable, Optional, Sequence

from endless_task.domain.models import (
    MemoryKind,
    MemoryProposal,
    MemoryProposalStatus,
    MemoryRecord,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now
from .sqlite_memory_repository import insert_memory_row, memory_record_from_row

Clock = Callable[[], str]


class SqliteMemoryProposalRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_content_chars: int = 1000,
        max_reason_chars: int = 200,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_content_chars <= 0 or max_reason_chars <= 0:
            raise ValueError("Proposal limits must be positive")
        self._database = database
        self._max_content_chars = max_content_chars
        self._max_reason_chars = max_reason_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        kind: MemoryKind,
        content: str,
        reason: str,
    ) -> MemoryProposal:
        normalized_kind = self._validate_kind(kind)
        normalized_content = self._validate_text(
            content, self._max_content_chars, field="content"
        )
        normalized_reason = self._validate_text(
            reason, self._max_reason_chars, field="reason"
        )
        if not conversation_id.strip() or not turn_id.strip():
            raise ValidationError("Proposal source conversation and turn are required.")

        existing = self.find_pending_by_content(normalized_content)
        if existing is not None:
            return existing

        now = self._clock()
        proposal_id = self._id_factory("memp")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_proposals (
                    id, conversation_id, turn_id, kind, content, reason,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    conversation_id.strip(),
                    turn_id.strip(),
                    normalized_kind.value,
                    normalized_content,
                    normalized_reason,
                    MemoryProposalStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> MemoryProposal:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Memory proposal not found: {proposal_id}")
        return self._from_row(row)

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[MemoryProposal]:
        query = "SELECT * FROM memory_proposals WHERE conversation_id = ?"
        params: list = [conversation_id]
        if not include_resolved:
            query += " AND status = ?"
            params.append(MemoryProposalStatus.PENDING.value)
        query += " ORDER BY created_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_pending_by_content(self, content: str) -> Optional[MemoryProposal]:
        normalized = content.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_proposals
                WHERE status = ? AND content = ?
                ORDER BY created_at, id
                """,
                (MemoryProposalStatus.PENDING.value, normalized),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def accept_proposal(
        self, proposal_id: str
    ) -> tuple[MemoryProposal, MemoryRecord]:
        now = self._clock()
        memory_id = self._id_factory("mem")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not MemoryProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            insert_memory_row(
                connection,
                memory_id=memory_id,
                kind=proposal.kind,
                content=proposal.content,
                source_conversation_id=proposal.conversation_id,
                source_turn_id=proposal.turn_id,
                timestamp=now,
            )
            connection.execute(
                """
                UPDATE memory_proposals
                SET status = ?, resolved_memory_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    MemoryProposalStatus.ACCEPTED.value,
                    memory_id,
                    now,
                    now,
                    proposal_id,
                ),
            )
            memory_row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self.get_proposal(proposal_id), memory_record_from_row(memory_row)

    def reject_proposal(self, proposal_id: str) -> MemoryProposal:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not MemoryProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            connection.execute(
                """
                UPDATE memory_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (MemoryProposalStatus.REJECTED.value, now, now, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def _validate_kind(self, kind: MemoryKind) -> MemoryKind:
        if isinstance(kind, MemoryKind):
            return kind
        try:
            return MemoryKind(kind)
        except ValueError as error:
            raise ValidationError(f"Unsupported memory kind: {kind!r}") from error

    def _validate_text(self, value: str, limit: int, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"Proposal {field} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValidationError(f"Proposal {field} must not be empty.")
        if len(normalized) > limit:
            raise ValidationError(f"Proposal {field} exceeds {limit} characters.")
        return normalized

    @staticmethod
    def _from_row(row) -> MemoryProposal:
        return MemoryProposal(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            kind=MemoryKind(row["kind"]),
            content=row["content"],
            reason=row["reason"],
            status=MemoryProposalStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_memory_id=row["resolved_memory_id"],
            resolved_at=row["resolved_at"],
        )
