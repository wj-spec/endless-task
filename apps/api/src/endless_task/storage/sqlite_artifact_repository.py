from __future__ import annotations

import json
from typing import Callable, Optional, Sequence, Tuple

from endless_task.domain.models import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactSnapshot,
    ArtifactStatus,
    ArtifactVersionOperation,
    ArtifactVersionRecord,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def artifact_record_from_row(row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        title=row["title"],
        kind=ArtifactKind(row["kind"]),
        status=ArtifactStatus(row["status"]),
        current_version_ordinal=row["current_version_ordinal"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _artifact_version_from_row(row) -> ArtifactVersionRecord:
    return ArtifactVersionRecord(
        id=row["id"],
        artifact_id=row["artifact_id"],
        ordinal=row["ordinal"],
        content=row["content"],
        operation=ArtifactVersionOperation(row["operation"]),
        source_conversation_id=row["source_conversation_id"],
        source_turn_id=row["source_turn_id"],
        source_labels=tuple(json.loads(row["source_labels"])),
        note=row["note"],
        created_at=row["created_at"],
    )


def insert_artifact_with_first_version(
    connection,
    *,
    artifact_id: str,
    version_id: str,
    title: str,
    kind: ArtifactKind,
    content: str,
    source_conversation_id: str,
    source_turn_id: str,
    timestamp: str,
    source_labels_json: str = "[]",
    note: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO artifacts (
            id, title, kind, status, current_version_ordinal,
            created_at, updated_at, deleted_at
        ) VALUES (?, ?, ?, 'active', 1, ?, ?, NULL)
        """,
        (artifact_id, title, kind.value, timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO artifact_versions (
            id, artifact_id, ordinal, content, operation,
            source_conversation_id, source_turn_id, source_labels,
            note, created_at
        ) VALUES (?, ?, 1, ?, 'create', ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            artifact_id,
            content,
            source_conversation_id,
            source_turn_id,
            source_labels_json,
            note,
            timestamp,
        ),
    )


class SqliteArtifactRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 200,
        max_content_chars: int = 200_000,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_title_chars <= 0:
            raise ValueError("max_title_chars must be positive")
        if max_content_chars <= 0:
            raise ValueError("max_content_chars must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_content_chars = max_content_chars
        self._clock = clock
        self._id_factory = id_factory
        self._embedding_hook = None

    # ---------- R5.8 索引钩子 ----------

    def set_embedding_hook(self, hook) -> None:
        """注入索引钩子（submit/remove），写侧增量建向量。"""
        self._embedding_hook = hook

    def _notify_hook(self, action: str, ref_id: str) -> None:
        hook = self._embedding_hook
        if hook is None:
            return
        try:
            if action == "submit":
                hook.submit('artifact', ref_id)
            else:
                hook.remove('artifact', ref_id)
        except Exception:  # noqa: BLE001 钩子失败不影响写操作
            pass

    def create_artifact(
        self,
        *,
        title: str,
        kind: ArtifactKind,
        content: str,
        source_conversation_id: str,
        source_turn_id: str,
        source_labels: Sequence[str] = (),
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        artifact_kind = self._validate_kind(kind)
        validated_title = self._validate_title(title)
        validated_content = self._validate_content(content)
        labels_json = self._validate_source_labels(source_labels)
        if not source_conversation_id or not source_turn_id:
            raise ValidationError("Artifact source conversation and turn are required.")
        now = self._clock()
        artifact_id = self._id_factory("art")
        version_id = self._id_factory("artv")
        with self._database.transaction() as connection:
            insert_artifact_with_first_version(
                connection,
                artifact_id=artifact_id,
                version_id=version_id,
                title=validated_title,
                kind=artifact_kind,
                content=validated_content,
                source_conversation_id=source_conversation_id,
                source_turn_id=source_turn_id,
                timestamp=now,
                source_labels_json=labels_json,
                note=note,
            )
        self._notify_hook("submit", artifact_id)
        return self.get_artifact_snapshot(artifact_id)

    def append_version(
        self,
        *,
        artifact_id: str,
        content: str,
        operation: ArtifactVersionOperation,
        source_conversation_id: str,
        source_turn_id: str,
        source_labels: Sequence[str] = (),
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        validated_operation = self._validate_append_operation(operation)
        validated_content = self._validate_content(content)
        labels_json = self._validate_source_labels(source_labels)
        if not source_conversation_id or not source_turn_id:
            raise ValidationError("Artifact source conversation and turn are required.")
        now = self._clock()
        version_id = self._id_factory("artv")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Artifact {artifact_id} was not found")
            if row["status"] != ArtifactStatus.ACTIVE.value:
                raise InvalidStateError("Deleted artifacts cannot receive versions")
            ordinal = row["current_version_ordinal"] + 1
            connection.execute(
                """
                INSERT INTO artifact_versions (
                    id, artifact_id, ordinal, content, operation,
                    source_conversation_id, source_turn_id, source_labels,
                    note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    artifact_id,
                    ordinal,
                    validated_content,
                    validated_operation.value,
                    source_conversation_id,
                    source_turn_id,
                    labels_json,
                    note,
                    now,
                ),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_ordinal = ?, updated_at = ? "
                "WHERE id = ?",
                (ordinal, now, artifact_id),
            )
        self._notify_hook("submit", artifact_id)
        return self.get_artifact_snapshot(artifact_id)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Artifact {artifact_id} was not found")
        return artifact_record_from_row(row)

    def get_current_version(self, artifact_id: str) -> ArtifactVersionRecord:
        artifact = self.get_artifact(artifact_id)
        return self.get_version(artifact_id, artifact.current_version_ordinal)

    def get_version(self, artifact_id: str, ordinal: int) -> ArtifactVersionRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? AND ordinal = ?",
                (artifact_id, ordinal),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Artifact {artifact_id} has no version {ordinal}"
            )
        return _artifact_version_from_row(row)

    def list_versions(self, artifact_id: str) -> Sequence[ArtifactVersionRecord]:
        self.get_artifact(artifact_id)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? "
                "ORDER BY ordinal ASC",
                (artifact_id,),
            ).fetchall()
        return tuple(_artifact_version_from_row(row) for row in rows)

    def list_artifacts(
        self, *, include_deleted: bool = False
    ) -> Sequence[ArtifactRecord]:
        query = "SELECT * FROM artifacts"
        if not include_deleted:
            query += " WHERE status = 'active'"
        query += " ORDER BY updated_at DESC, id ASC"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(artifact_record_from_row(row) for row in rows)

    def list_artifacts_for_conversation(
        self, conversation_id: str
    ) -> Sequence[ArtifactRecord]:
        query = (
            "SELECT a.* FROM artifacts a "
            "WHERE a.status = 'active' AND EXISTS ("
            "SELECT 1 FROM artifact_versions v "
            "WHERE v.artifact_id = a.id AND v.source_conversation_id = ?"
            ") ORDER BY a.updated_at DESC, a.id ASC"
        )
        with self._database.connect() as connection:
            rows = connection.execute(query, (conversation_id,)).fetchall()
        return tuple(artifact_record_from_row(row) for row in rows)

    def delete_artifact(self, artifact_id: str) -> ArtifactRecord:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Artifact {artifact_id} was not found")
            if row["status"] != ArtifactStatus.ACTIVE.value:
                raise InvalidStateError("Artifact was already deleted")
            connection.execute(
                "UPDATE artifacts SET status = 'deleted', deleted_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, artifact_id),
            )
        self._notify_hook("remove", artifact_id)
        return self.get_artifact(artifact_id)

    def rollback_to_version(
        self,
        *,
        artifact_id: str,
        target_ordinal: int,
        source_conversation_id: str,
        source_turn_id: str,
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        if (
            isinstance(target_ordinal, bool)
            or not isinstance(target_ordinal, int)
            or target_ordinal < 1
        ):
            raise InvalidStateError(
                "Rollback target ordinal must be a positive integer"
            )
        if not source_conversation_id or not source_conversation_id.strip():
            raise ValidationError("Rollback source conversation is required.")
        if not source_turn_id or not source_turn_id.strip():
            raise ValidationError("Rollback source turn is required.")
        now = self._clock()
        version_id = self._id_factory("artv")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Artifact {artifact_id} was not found")
            if row["status"] != ArtifactStatus.ACTIVE.value:
                raise InvalidStateError("Deleted artifacts cannot be rolled back")
            current_ordinal = row["current_version_ordinal"]
            if target_ordinal >= current_ordinal:
                raise InvalidStateError(
                    "Rollback target must be earlier than the current version"
                )
            target_row = connection.execute(
                "SELECT * FROM artifact_versions "
                "WHERE artifact_id = ? AND ordinal = ?",
                (artifact_id, target_ordinal),
            ).fetchone()
            if target_row is None:
                raise NotFoundError(
                    f"Artifact {artifact_id} has no version {target_ordinal}"
                )
            ordinal = current_ordinal + 1
            resolved_note = (
                note.strip()
                if isinstance(note, str) and note.strip()
                else f"回滚到版本 {target_ordinal}"
            )
            connection.execute(
                """
                INSERT INTO artifact_versions (
                    id, artifact_id, ordinal, content, operation,
                    source_conversation_id, source_turn_id, source_labels,
                    note, created_at
                ) VALUES (?, ?, ?, ?, 'rollback', ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    artifact_id,
                    ordinal,
                    target_row["content"],
                    source_conversation_id.strip(),
                    source_turn_id.strip(),
                    target_row["source_labels"],
                    resolved_note,
                    now,
                ),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_ordinal = ?, updated_at = ? "
                "WHERE id = ?",
                (ordinal, now, artifact_id),
            )
        self._notify_hook("submit", artifact_id)
        return self.get_artifact_snapshot(artifact_id)

    def get_artifact_snapshot(self, artifact_id: str) -> ArtifactSnapshot:
        artifact = self.get_artifact(artifact_id)
        return ArtifactSnapshot(
            artifact=artifact,
            current_version=self.get_version(
                artifact_id, artifact.current_version_ordinal
            ),
        )

    def _validate_kind(self, kind) -> ArtifactKind:
        try:
            return ArtifactKind(kind)
        except ValueError as error:
            raise ValidationError(f"Unsupported artifact kind: {kind!r}") from error

    def _validate_title(self, title: str) -> str:
        if not isinstance(title, str):
            raise ValidationError("Artifact title must be a string.")
        if not title.strip():
            raise ValidationError("Artifact title must not be empty.")
        if len(title) > self._max_title_chars:
            raise ValidationError(
                f"Artifact title must be at most {self._max_title_chars} characters."
            )
        return title

    def _validate_content(self, content: str) -> str:
        if not isinstance(content, str):
            raise ValidationError("Artifact content must be a string.")
        if not content.strip():
            raise ValidationError("Artifact content must not be empty.")
        if len(content) > self._max_content_chars:
            raise ValidationError(
                f"Artifact content must be at most {self._max_content_chars} characters."
            )
        return content

    def _validate_append_operation(
        self, operation
    ) -> ArtifactVersionOperation:
        try:
            validated = ArtifactVersionOperation(operation)
        except ValueError as error:
            raise ValidationError(
                f"Unsupported artifact version operation: {operation!r}"
            ) from error
        if validated is not ArtifactVersionOperation.UPDATE:
            raise ValidationError(
                "Only update versions can be appended in this stage."
            )
        return validated

    @staticmethod
    def _validate_source_labels(source_labels: Sequence[str]) -> str:
        try:
            labels = list(source_labels)
        except TypeError as error:
            raise ValidationError("Artifact source labels must be a sequence.") from error
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                raise ValidationError(
                    "Artifact source labels must be non-empty strings."
                )
        return json.dumps(labels, ensure_ascii=False, separators=(",", ":"))
