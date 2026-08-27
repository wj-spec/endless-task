from __future__ import annotations

import json
from typing import Callable, Optional, Sequence, Tuple

from endless_task.domain.models import (
    ArtifactKind,
    ArtifactProposal,
    ArtifactProposalStatus,
    ArtifactRecord,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_artifact_repository import (
    artifact_record_from_row,
    insert_artifact_with_first_version,
)

from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


class SqliteArtifactProposalRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 200,
        max_content_chars: int = 200_000,
        max_reason_chars: int = 200,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
        artifact_store=None,
    ) -> None:
        if max_title_chars <= 0 or max_content_chars <= 0 or max_reason_chars <= 0:
            raise ValueError("Proposal limits must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_content_chars = max_content_chars
        self._max_reason_chars = max_reason_chars
        self._clock = clock
        self._id_factory = id_factory
        self._artifact_store = artifact_store

    def set_artifact_store(self, store) -> None:
        self._artifact_store = store

    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        title: str,
        kind: ArtifactKind,
        content: str,
        reason: str,
        source_labels: Sequence[str] = (),
        target_artifact_id: Optional[str] = None,
        base_version_ordinal: Optional[int] = None,
    ) -> ArtifactProposal:
        normalized_kind = self._validate_kind(kind)
        normalized_title = self._validate_text(
            title, self._max_title_chars, field="title"
        )
        normalized_content = self._validate_text(
            content, self._max_content_chars, field="content"
        )
        normalized_reason = self._validate_text(
            reason, self._max_reason_chars, field="reason"
        )
        if not conversation_id.strip() or not turn_id.strip():
            raise ValidationError("Proposal source conversation and turn are required.")
        if base_version_ordinal is not None and (
            not isinstance(base_version_ordinal, int)
            or isinstance(base_version_ordinal, bool)
            or base_version_ordinal < 1
        ):
            raise ValidationError("Proposal base version ordinal must be positive.")
        labels_json = self._validate_source_labels(source_labels)

        existing = self.find_pending_by_content(normalized_content)
        if existing is not None:
            return existing

        now = self._clock()
        proposal_id = self._id_factory("artp")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifact_proposals (
                    id, conversation_id, turn_id, title, kind, content, reason,
                    status, created_at, updated_at, source_labels,
                    target_artifact_id, base_version_ordinal
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    conversation_id.strip(),
                    turn_id.strip(),
                    normalized_title,
                    normalized_kind.value,
                    normalized_content,
                    normalized_reason,
                    ArtifactProposalStatus.PENDING.value,
                    now,
                    now,
                    labels_json,
                    target_artifact_id,
                    base_version_ordinal,
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> ArtifactProposal:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Artifact proposal not found: {proposal_id}")
        return self._from_row(row)

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[ArtifactProposal]:
        query = "SELECT * FROM artifact_proposals WHERE conversation_id = ?"
        params: list = [conversation_id]
        if not include_resolved:
            query += " AND status = ?"
            params.append(ArtifactProposalStatus.PENDING.value)
        query += " ORDER BY created_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_pending(self, *, limit: int) -> Sequence[ArtifactProposal]:
        query = (
            "SELECT * FROM artifact_proposals WHERE status = ? "
            "ORDER BY created_at, id LIMIT ?"
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                query, (ArtifactProposalStatus.PENDING.value, limit)
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_pending_by_content(self, content: str) -> Optional[ArtifactProposal]:
        normalized = content.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM artifact_proposals
                WHERE status = ? AND content = ?
                ORDER BY created_at, id
                """,
                (ArtifactProposalStatus.PENDING.value, normalized),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def accept_proposal(
        self, proposal_id: str
    ) -> Tuple[ArtifactProposal, ArtifactRecord]:
        from endless_task.workspace_runtime.artifact_store import (
            plan_new,
            plan_update,
        )
        now = self._clock()
        artifact_id = self._id_factory("art")
        version_id = self._id_factory("artv")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Artifact proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not ArtifactProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            labels_json = json.dumps(
                list(proposal.source_labels),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            plan = None
            previous_content: Optional[str] = None
            if proposal.target_artifact_id:
                artifact_id = proposal.target_artifact_id
                target_row = connection.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if target_row is None:
                    raise InvalidStateError("Proposal target artifact was not found")
                if target_row["status"] != "active":
                    raise InvalidStateError("Proposal target artifact was deleted")
                if (
                    proposal.base_version_ordinal is not None
                    and target_row["current_version_ordinal"]
                    != proposal.base_version_ordinal
                ):
                    raise InvalidStateError(
                        "Proposal base version is stale; regenerate from the "
                        "latest artifact content"
                    )
                ordinal = target_row["current_version_ordinal"] + 1
                if self._artifact_store is not None:
                    binding = self._artifact_store.binding_for(
                        proposal.conversation_id
                    )
                    if binding is not None:
                        plan = plan_update(
                            binding,
                            artifact_id,
                            proposal.title,
                            proposal.kind,
                            proposal.content,
                            new_ordinal=ordinal,
                            existing_storage_path=target_row["storage_path"],
                        )
                        previous_row = connection.execute(
                            "SELECT content FROM artifact_versions "
                            "WHERE artifact_id = ? AND ordinal = ?",
                            (
                                artifact_id,
                                target_row["current_version_ordinal"],
                            ),
                        ).fetchone()
                        previous_content = (
                            previous_row["content"] if previous_row else None
                        )
                connection.execute(
                    """
                    INSERT INTO artifact_versions (
                        id, artifact_id, ordinal, content, operation,
                        source_conversation_id, source_turn_id, source_labels,
                        note, created_at, storage_path, content_sha256
                    ) VALUES (?, ?, ?, ?, 'chat_continue', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        artifact_id,
                        ordinal,
                        proposal.content,
                        proposal.conversation_id,
                        proposal.turn_id,
                        labels_json,
                        proposal.reason,
                        now,
                        plan.version_storage_path if plan else None,
                        plan.content_sha256 if plan else None,
                    ),
                )
                connection.execute(
                    "UPDATE artifacts SET current_version_ordinal = ?, "
                    "updated_at = ? WHERE id = ?",
                    (ordinal, now, artifact_id),
                )
                if (
                    plan is not None
                    and target_row["storage_path"] is None
                ):
                    # 惰性迁移：存量数据库全文 Artifact 首次在工作区会话更新时落盘
                    connection.execute(
                        "UPDATE artifacts SET storage_path = ?, content_sha256 = ? "
                        "WHERE id = ?",
                        (
                            plan.storage_path,
                            plan.content_sha256,
                            artifact_id,
                        ),
                    )
            else:
                if self._artifact_store is not None:
                    binding = self._artifact_store.binding_for(
                        proposal.conversation_id
                    )
                    if binding is not None:
                        plan = plan_new(
                            binding,
                            artifact_id,
                            proposal.title,
                            proposal.kind,
                            proposal.content,
                        )
                insert_artifact_with_first_version(
                    connection,
                    artifact_id=artifact_id,
                    version_id=version_id,
                    title=proposal.title,
                    kind=proposal.kind,
                    content=proposal.content,
                    source_conversation_id=proposal.conversation_id,
                    source_turn_id=proposal.turn_id,
                    timestamp=now,
                    source_labels_json=labels_json,
                    storage_path=plan.storage_path if plan else None,
                    content_sha256=plan.content_sha256 if plan else None,
                )
            connection.execute(
                """
                UPDATE artifact_proposals
                SET status = ?, resolved_artifact_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    ArtifactProposalStatus.ACCEPTED.value,
                    artifact_id,
                    now,
                    now,
                    proposal_id,
                ),
            )
            artifact_row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if (
            plan is not None
            and self._artifact_store is not None
            and artifact_row is not None
        ):
            binding = self._artifact_store.binding_for(proposal.conversation_id)
            if binding is not None:
                try:
                    self._artifact_store.materialize(
                        binding,
                        plan,
                        proposal.content,
                        previous_content=previous_content,
                    )
                except Exception:  # noqa: BLE001 文件落盘失败不阻断 DB（内容已双写）
                    pass
        return self.get_proposal(proposal_id), artifact_record_from_row(artifact_row)

    def reject_proposal(self, proposal_id: str) -> ArtifactProposal:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Artifact proposal not found: {proposal_id}")
            proposal = self._from_row(row)
            if proposal.status is not ArtifactProposalStatus.PENDING:
                raise InvalidStateError("Only pending proposals can be resolved.")
            connection.execute(
                """
                UPDATE artifact_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ArtifactProposalStatus.REJECTED.value, now, now, proposal_id),
            )
        return self.get_proposal(proposal_id)

    @staticmethod
    def _validate_source_labels(source_labels: Sequence[str]) -> str:
        try:
            labels = list(source_labels)
        except TypeError as error:
            raise ValidationError("Proposal source labels must be a sequence.") from error
        if len(labels) > MAX_SOURCE_LABELS:
            raise ValidationError(
                f"Proposal source labels exceed {MAX_SOURCE_LABELS} entries."
            )
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                raise ValidationError(
                    "Proposal source labels must be non-empty strings."
                )
        return json.dumps(labels, ensure_ascii=False, separators=(",", ":"))

    def _validate_kind(self, kind) -> ArtifactKind:
        if isinstance(kind, ArtifactKind):
            return kind
        try:
            return ArtifactKind(kind)
        except ValueError as error:
            raise ValidationError(f"Unsupported artifact kind: {kind!r}") from error

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
    def _from_row(row) -> ArtifactProposal:
        return ArtifactProposal(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            title=row["title"],
            kind=ArtifactKind(row["kind"]),
            content=row["content"],
            reason=row["reason"],
            status=ArtifactProposalStatus(row["status"]),
            source_labels=tuple(json.loads(row["source_labels"])),
            target_artifact_id=row["target_artifact_id"],
            base_version_ordinal=row["base_version_ordinal"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_artifact_id=row["resolved_artifact_id"],
            resolved_at=row["resolved_at"],
        )


# 延迟到文件底部导入，避免 storage ↔ artifacts 包级循环导入。
from endless_task.artifacts.source_labels import MAX_SOURCE_LABELS  # noqa: E402
