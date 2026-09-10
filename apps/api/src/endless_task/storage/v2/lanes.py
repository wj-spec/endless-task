"""lane 与临时会话：树结构、归档/恢复/提升、会话指针。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）：方法体一字未改，
只是换了宿主类。共享的 `self._database` / `self._write` / 行映射由
`V2RepositoryBase` 提供。
"""

from __future__ import annotations

from .base import V2RepositoryBase


from .base import _dump, _load
from collections.abc import Mapping
from endless_task.domain.models import ConversationKind
from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError
from endless_task.runtime_v2.domain import ConversationPointer, LaneKind, LanePromotionRecord, LaneRecord, LaneStatus, TemporaryConversationRecord
import sqlite3
from typing import Any, Optional


class LaneRepositoryMixin(V2RepositoryBase):
    """见模块 docstring。"""

    def create_lane(
        self,
        *,
        conversation_id: str,
        kind: LaneKind = LaneKind.MAIN,
        base_entry_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
        status: LaneStatus = LaneStatus.ACTIVE,
        archived_at: Optional[str] = None,
        display_name: Optional[str] = None,
        summary: Optional[str] = None,
        source_lane_id: Optional[str] = None,
        created_from_entry_id: Optional[str] = None,
    ) -> LaneRecord:
        lane_id = lane_id or self._id_factory("lane")
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None
        persisted_kind = kind
        if kind is LaneKind.ARCHIVED:
            persisted_kind = LaneKind.PERSISTENT_BRANCH
            status = LaneStatus.ARCHIVED
            archived_at = archived_at or now
        metadata_dict = dict(metadata or {})
        if source_lane_id is None:
            raw_source_lane_id = metadata_dict.get("sourceLaneId")
            source_lane_id = raw_source_lane_id if isinstance(raw_source_lane_id, str) else None
        created_from_entry_id = created_from_entry_id or base_entry_id
        if persisted_kind is LaneKind.MAIN and status is LaneStatus.ARCHIVED:
            raise InvalidStateError("The main lane cannot be archived")

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            self._ensure_conversation(connection, conversation_id)
            if base_entry_id is not None:
                base = self._get_entry_row(connection, base_entry_id)
                if base["conversation_id"] != conversation_id:
                    raise InvalidStateError("Base entry does not belong to conversation")
            if source_lane_id is not None:
                self._get_lane_row(connection, source_lane_id)
            if created_from_entry_id is not None:
                self._get_entry_row(connection, created_from_entry_id)
            existing = connection.execute(
                "SELECT 1 FROM v2_lanes WHERE conversation_id = ? AND kind = 'main'",
                (conversation_id,),
            ).fetchone()
            if persisted_kind is LaneKind.MAIN and existing is not None:
                raise ConflictError("Conversation already has a main lane")

            connection.execute(
                """
                INSERT INTO v2_lanes(
                    id, conversation_id, kind, base_entry_id, leaf_entry_id,
                    created_at, metadata_json, status, archived_at,
                    display_name, summary, source_lane_id, created_from_entry_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lane_id,
                    conversation_id,
                    persisted_kind.value,
                    base_entry_id,
                    base_entry_id,
                    now,
                    _dump(metadata_dict),
                    status.value,
                    archived_at,
                    display_name,
                    summary,
                    source_lane_id,
                    created_from_entry_id,
                ),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=conversation_id,
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def get_lane(self, lane_id: str) -> LaneRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM v2_lanes WHERE id = ?", (lane_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Lane not found: {lane_id}")
        return self._lane_from_row(row)

    def list_lanes(
        self,
        conversation_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[LaneRecord, ...]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            archived_filter = "" if include_archived else "AND kind != 'archived' AND status != 'archived'"
            rows = connection.execute(
                f"""
                SELECT * FROM v2_lanes
                WHERE conversation_id = ? {archived_filter}
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(self._lane_from_row(row) for row in rows)

    def set_conversation_pointer(
        self,
        *,
        conversation_id: str,
        active_lane_id: str,
        active_run_id: Optional[str] = None,
        active_run_variant_id: Optional[str] = None,
    ) -> ConversationPointer:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ConversationPointer:
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, active_lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == LaneKind.ARCHIVED.value or lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot become active")
            if active_run_id is not None:
                run = self._get_run_row(connection, active_run_id)
                if run["conversation_id"] != conversation_id:
                    raise InvalidStateError("Run does not belong to conversation")
                if run["lane_id"] != active_lane_id:
                    raise InvalidStateError("Run does not belong to active lane")
            if active_run_variant_id is not None:
                run = self._get_run_row(connection, active_run_variant_id)
                if run["conversation_id"] != conversation_id:
                    raise InvalidStateError("Run variant does not belong to conversation")
                if run["lane_id"] != active_lane_id:
                    raise InvalidStateError("Run variant does not belong to active lane")

            connection.execute(
                """
                INSERT INTO v2_conversation_pointers(
                    conversation_id, active_lane_id, active_run_id,
                    active_run_variant_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    active_lane_id = excluded.active_lane_id,
                    active_run_id = excluded.active_run_id,
                    active_run_variant_id = excluded.active_run_variant_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    active_lane_id,
                    active_run_id,
                    active_run_variant_id,
                    now,
                ),
            )
            return ConversationPointer(
                conversation_id=conversation_id,
                active_lane_id=active_lane_id,
                active_run_id=active_run_id,
                active_run_variant_id=active_run_variant_id,
                updated_at=now,
            )

        return self._write(operation)

    def get_conversation_pointer(self, conversation_id: str) -> Optional[ConversationPointer]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            row = connection.execute(
                "SELECT * FROM v2_conversation_pointers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationPointer(
            conversation_id=row["conversation_id"],
            active_lane_id=row["active_lane_id"],
            active_run_id=row["active_run_id"],
            active_run_variant_id=row["active_run_variant_id"],
            updated_at=row["updated_at"],
        )

    def update_lane_kind(self, lane_id: str, kind: LaneKind) -> LaneRecord:
        if kind is LaneKind.ARCHIVED:
            return self.archive_lane_subtree(lane_id)[0]

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            lane = self._get_lane_row(connection, lane_id)
            current_kind = LaneKind(lane["kind"])
            if current_kind is LaneKind.ARCHIVED:
                current_kind = LaneKind.PERSISTENT_BRANCH
            if current_kind is kind:
                return self._lane_from_row(lane)
            if current_kind is LaneKind.MAIN:
                raise InvalidStateError("Use lane promotion to replace the main lane")
            if kind is LaneKind.MAIN:
                raise InvalidStateError("Use lane promotion to make a lane main")
            if lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot change kind")
            connection.execute(
                "UPDATE v2_lanes SET kind = ? WHERE id = ?",
                (kind.value, lane_id),
            )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def rename_lane(
        self,
        lane_id: str,
        display_name: Optional[str],
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> LaneRecord:
        normalized = " ".join((display_name or "").split()) or None
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            lane = self._get_lane_row(connection, lane_id)
            previous_display_name = lane["display_name"]
            if previous_display_name == normalized:
                return self._lane_from_row(lane)
            connection.execute(
                "UPDATE v2_lanes SET display_name = ? WHERE id = ?",
                (normalized, lane_id),
            )
            if lane_event_type is not None and lane_event_id is not None:
                event_data = dict(lane_event_data or {})
                event_data.update(
                    {
                        "laneId": lane_id,
                        "displayName": normalized,
                        "previousDisplayName": previous_display_name,
                    }
                )
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=event_data,
                )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def archive_lane_subtree(
        self,
        lane_id: str,
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> tuple[LaneRecord, ...]:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> tuple[LaneRecord, ...]:
            lane = self._get_lane_row(connection, lane_id)
            if lane["kind"] == LaneKind.TEMPORARY.value:
                raise InvalidStateError("Temporary conversation lanes cannot be archived")
            pointer = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (lane["conversation_id"],),
            ).fetchone()
            is_main = (
                pointer["active_lane_id"] == lane_id
                if pointer is not None
                else lane["kind"] == LaneKind.MAIN.value
            )
            if is_main:
                raise InvalidStateError("The main lane cannot be archived")
            subtree = self._lane_subtree_rows(
                connection,
                conversation_id=lane["conversation_id"],
                lane_id=lane_id,
                excluded_lane_id=pointer["active_lane_id"] if pointer is not None else None,
            )
            lane_ids = tuple(row["id"] for row in subtree)
            active = connection.execute(
                f"""
                SELECT 1 FROM v2_runs
                WHERE lane_id IN ({','.join('?' for _ in lane_ids)})
                  AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                  )
                LIMIT 1
                """,
                lane_ids,
            ).fetchone()
            if active is not None:
                raise ConflictError("A lane with an active run cannot be archived")
            connection.execute(
                f"""
                UPDATE v2_lanes
                SET status = 'archived', archived_at = COALESCE(archived_at, ?),
                    kind = CASE WHEN kind = 'archived' THEN 'persistent_branch' ELSE kind END
                WHERE id IN ({','.join('?' for _ in lane_ids)})
                """,
                (now, *lane_ids),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return tuple(self._lane_from_row(self._get_lane_row(connection, item)) for item in lane_ids)

        return self._write(operation)

    def restore_lane_subtree(
        self,
        lane_id: str,
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> tuple[LaneRecord, ...]:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> tuple[LaneRecord, ...]:
            lane = self._get_lane_row(connection, lane_id)
            pointer = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (lane["conversation_id"],),
            ).fetchone()
            subtree = self._lane_subtree_rows(
                connection,
                conversation_id=lane["conversation_id"],
                lane_id=lane_id,
                excluded_lane_id=pointer["active_lane_id"] if pointer is not None else None,
            )
            lane_ids = tuple(row["id"] for row in subtree)
            connection.execute(
                f"""
                UPDATE v2_lanes
                SET status = 'active', archived_at = NULL,
                    kind = CASE WHEN kind = 'archived' THEN 'persistent_branch' ELSE kind END
                WHERE id IN ({','.join('?' for _ in lane_ids)})
                """,
                lane_ids,
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return tuple(self._lane_from_row(self._get_lane_row(connection, item)) for item in lane_ids)

        return self._write(operation)

    def promote_lane(
        self,
        *,
        conversation_id: str,
        target_lane_id: str,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> LanePromotionRecord:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> LanePromotionRecord:
            self._ensure_conversation(connection, conversation_id)
            pointer_row = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if pointer_row is None:
                raise ConflictError("Conversation has no v2 lane pointer")
            previous_main = self._get_lane_row(
                connection,
                pointer_row["active_lane_id"],
            )
            target = self._get_lane_row(connection, target_lane_id)
            if previous_main["conversation_id"] != conversation_id:
                raise InvalidStateError("Active lane does not belong to conversation")
            if target["conversation_id"] != conversation_id:
                raise InvalidStateError("Target lane does not belong to conversation")
            if target_lane_id == pointer_row["active_lane_id"]:
                raise ConflictError("Target lane is already active")
            if target["kind"] == LaneKind.ARCHIVED.value or target["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot be promoted")

            connection.execute(
                """
                UPDATE v2_lanes
                SET kind = CASE
                    WHEN id = ? THEN 'main'
                    WHEN kind = 'main' THEN 'persistent_branch'
                    ELSE kind
                END
                WHERE conversation_id = ?
                """,
                (target_lane_id, conversation_id),
            )
            connection.execute(
                """
                UPDATE v2_conversation_pointers
                SET active_lane_id = ?, active_run_id = NULL,
                    active_run_variant_id = NULL, updated_at = ?
                WHERE conversation_id = ?
                """,
                (target_lane_id, now, conversation_id),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=conversation_id,
                    lane_id=target_lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            promoted = self._lane_from_row(self._get_lane_row(connection, target_lane_id))
            previous = self._lane_from_row(self._get_lane_row(connection, previous_main["id"]))
            pointer = ConversationPointer(
                conversation_id=conversation_id,
                active_lane_id=target_lane_id,
                active_run_id=None,
                active_run_variant_id=None,
                updated_at=now,
            )
            return LanePromotionRecord(
                promoted_lane=promoted,
                previous_main_lane=previous,
                pointer=pointer,
            )

        return self._write(operation)

    def create_temporary_conversation_from_lane(
        self,
        *,
        source_conversation_id: str,
        source_lane_id: str,
        source_leaf_entry_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[str, LaneRecord, TemporaryConversationRecord]:
        temporary_conversation_id = conversation_id or self._id_factory("conv")
        temporary_lane_id = lane_id or self._id_factory("lane")
        now = self._clock()

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[str, LaneRecord, TemporaryConversationRecord]:
            source_conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (source_conversation_id,),
            ).fetchone()
            if source_conversation is None:
                raise NotFoundError(f"Conversation not found: {source_conversation_id}")
            if source_conversation["kind"] == ConversationKind.EPHEMERAL.value:
                raise InvalidStateError("Temporary conversations cannot be forked")
            active = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ? AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                )
                LIMIT 1
                """,
                (source_conversation_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("Conversation has an active run")

            source_lane = self._get_lane_row(connection, source_lane_id)
            if source_lane["conversation_id"] != source_conversation_id:
                raise InvalidStateError("Source lane does not belong to conversation")
            if source_lane["kind"] == LaneKind.ARCHIVED.value or source_lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot create temporary conversations")
            leaf_entry_id = source_leaf_entry_id or source_lane["leaf_entry_id"]
            if leaf_entry_id is None or source_lane["leaf_entry_id"] is None:
                raise ConflictError("Source lane has no visible entries")
            visible_source_entries = self._entry_context_rows(
                connection,
                source_lane["leaf_entry_id"],
                include_variants=False,
            )
            visible_source_entry_ids = {row["id"] for row in visible_source_entries}
            if leaf_entry_id not in visible_source_entry_ids:
                raise InvalidStateError("Source entry is not on the source lane path")
            source_entries = self._entry_context_rows(
                connection,
                leaf_entry_id,
                include_variants=False,
            )
            source_entry_ids = tuple(row["id"] for row in source_entries)
            source_title = " ".join((title or "").split()) or "临时对话"
            connection.execute(
                """
                INSERT INTO conversations(
                    id, title, status, next_turn_ordinal, title_is_manual,
                    created_at, updated_at, archived_at,
                    parent_conversation_id, fork_turn_id, kind, promoted_at,
                    workspace_id, provider_profile_id, model_override
                ) VALUES (?, ?, 'active', 1, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    temporary_conversation_id,
                    source_title,
                    1 if title and title.strip() else 0,
                    now,
                    now,
                    ConversationKind.EPHEMERAL.value,
                    source_conversation["workspace_id"],
                    source_conversation["provider_profile_id"],
                    source_conversation["model_override"],
                ),
            )
            connection.execute(
                """
                INSERT INTO v2_lanes(
                    id, conversation_id, kind, base_entry_id, leaf_entry_id,
                    created_at, metadata_json, status, archived_at,
                    display_name, summary, source_lane_id, created_from_entry_id
                )
                VALUES (?, ?, 'temporary', NULL, NULL, ?, ?, 'active', NULL, NULL, ?, ?, ?)
                """,
                (
                    temporary_lane_id,
                    temporary_conversation_id,
                    now,
                    _dump(
                        {
                            "sourceConversationId": source_conversation_id,
                            "sourceLaneId": source_lane_id,
                            "sourceLeafEntryId": leaf_entry_id,
                        }
                    ),
                    self._entry_excerpt(source_entries[-1]) if source_entries else None,
                    source_lane_id,
                    leaf_entry_id,
                ),
            )

            copied_ids: dict[str, str] = {}
            copied_entry_ids: list[str] = []
            for seq, source_entry in enumerate(source_entries, start=1):
                copied_entry_id = self._id_factory("entry")
                copied_ids[source_entry["id"]] = copied_entry_id
                copied_entry_ids.append(copied_entry_id)
                source_parent_id = source_entry["parent_id"]
                parent_id = copied_ids.get(source_parent_id) if source_parent_id else None
                display = _load(source_entry["display_json"])
                display.setdefault("sourceEntryId", source_entry["id"])
                connection.execute(
                    """
                    INSERT INTO v2_transcript_entries(
                        id, conversation_id, parent_id, lane_id, seq, type,
                        type_version, actor, status, created_at, updated_at,
                        payload_json, context_policy_json, display_json, source_run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        copied_entry_id,
                        temporary_conversation_id,
                        parent_id,
                        temporary_lane_id,
                        seq,
                        source_entry["type"],
                        source_entry["type_version"],
                        source_entry["actor"],
                        source_entry["status"],
                        source_entry["created_at"],
                        source_entry["updated_at"],
                        source_entry["payload_json"],
                        source_entry["context_policy_json"],
                        _dump(display),
                    ),
                )

            copied_leaf_entry_id = copied_ids[leaf_entry_id]
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (copied_leaf_entry_id, temporary_lane_id),
            )
            connection.execute(
                """
                INSERT INTO v2_conversation_pointers(
                    conversation_id, active_lane_id, active_run_id,
                    active_run_variant_id, updated_at
                ) VALUES (?, ?, NULL, NULL, ?)
                """,
                (temporary_conversation_id, temporary_lane_id, now),
            )
            connection.execute(
                """
                INSERT INTO v2_temporary_conversations(
                    conversation_id, source_conversation_id, source_lane_id,
                    source_base_entry_id, source_leaf_entry_id,
                    snapshot_entry_ids_json, created_at, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    temporary_conversation_id,
                    source_conversation_id,
                    source_lane_id,
                    source_lane["base_entry_id"],
                    leaf_entry_id,
                    _dump(
                        {
                            "entryIds": list(source_entry_ids),
                            "copiedEntryIds": copied_entry_ids,
                        }
                    ),
                    now,
                ),
            )
            self._insert_lane_event(
                connection,
                event_id=self._id_factory("lane_event"),
                conversation_id=temporary_conversation_id,
                lane_id=temporary_lane_id,
                event_type="temporary_conversation.created",
                occurred_at=now,
                data={
                    "sourceConversationId": source_conversation_id,
                    "sourceLaneId": source_lane_id,
                    "sourceLeafEntryId": leaf_entry_id,
                },
            )
            return (
                temporary_conversation_id,
                self._lane_from_row(self._get_lane_row(connection, temporary_lane_id)),
                self._temporary_conversation_from_row(
                    self._get_temporary_conversation_row(
                        connection,
                        temporary_conversation_id,
                    )
                ),
            )

        return self._write(operation)

    def get_temporary_conversation(
        self,
        conversation_id: str,
    ) -> TemporaryConversationRecord:
        with self._database.connect() as connection:
            row = self._get_temporary_conversation_row(connection, conversation_id)
        return self._temporary_conversation_from_row(row)

    def promote_temporary_conversation(
        self,
        conversation_id: str,
    ) -> TemporaryConversationRecord:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event")

        def operation(connection: sqlite3.Connection) -> TemporaryConversationRecord:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError(f"Conversation not found: {conversation_id}")
            if conversation["kind"] != ConversationKind.EPHEMERAL.value:
                raise ConflictError("Only temporary conversations can be promoted")
            temporary_record = self._get_temporary_conversation_row(
                connection,
                conversation_id,
            )
            active = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ? AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                )
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("Conversation has an active run")
            root = connection.execute(
                """
                SELECT * FROM v2_lanes
                WHERE conversation_id = ? AND kind = 'temporary'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if root is None:
                raise InvalidStateError("Temporary conversation root lane is missing")
            connection.execute(
                """
                UPDATE conversations
                SET kind = 'normal', promoted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, conversation_id),
            )
            connection.execute(
                "UPDATE v2_lanes SET kind = 'main' WHERE id = ?",
                (root["id"],),
            )
            connection.execute(
                """
                UPDATE v2_temporary_conversations
                SET promoted_at = COALESCE(promoted_at, ?)
                WHERE conversation_id = ?
                """,
                (now, conversation_id),
            )
            self._insert_lane_event(
                connection,
                event_id=lane_event_id,
                conversation_id=conversation_id,
                lane_id=root["id"],
                event_type="temporary_conversation.promoted",
                occurred_at=now,
                data={
                    "temporaryConversationId": conversation_id,
                    "sourceConversationId": temporary_record["source_conversation_id"],
                    "sourceLaneId": temporary_record["source_lane_id"],
                    "sourceLeafEntryId": temporary_record["source_leaf_entry_id"],
                },
            )
            return self._temporary_conversation_from_row(
                self._get_temporary_conversation_row(connection, conversation_id)
            )

        return self._write(operation)

    def delete_temporary_conversation(self, conversation_id: str) -> None:
        lane_event_id = self._id_factory("lane_event")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError(f"Conversation not found: {conversation_id}")
            temporary_record = self._get_temporary_conversation_row(
                connection,
                conversation_id,
            )
            if conversation["kind"] != ConversationKind.EPHEMERAL.value:
                raise ConflictError("Only unpromoted temporary conversations can be deleted")
            active = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ? AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                )
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("Conversation has an active run")
            root = connection.execute(
                """
                SELECT id FROM v2_lanes
                WHERE conversation_id = ? AND kind = 'temporary'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            source_conversation_id = temporary_record["source_conversation_id"]
            source_lane_id = temporary_record["source_lane_id"]
            if source_conversation_id is not None and source_lane_id is not None:
                source_lane = connection.execute(
                    """
                    SELECT id FROM v2_lanes
                    WHERE id = ? AND conversation_id = ?
                    """,
                    (source_lane_id, source_conversation_id),
                ).fetchone()
                if source_lane is not None:
                    self._insert_lane_event(
                        connection,
                        event_id=lane_event_id,
                        conversation_id=source_conversation_id,
                        lane_id=source_lane_id,
                        event_type="temporary_conversation.deleted",
                        occurred_at=now,
                        data={
                            "temporaryConversationId": conversation_id,
                            "temporaryLaneId": root["id"] if root is not None else None,
                            "sourceConversationId": source_conversation_id,
                            "sourceLaneId": source_lane_id,
                            "sourceLeafEntryId": temporary_record["source_leaf_entry_id"],
                        },
                    )
            connection.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )

        self._write(operation)
