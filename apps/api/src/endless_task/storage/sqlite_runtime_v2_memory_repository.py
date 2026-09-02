from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional

from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError

from endless_task.runtime_v2.domain import (
    MemoryPromotionStatus,
    MemoryScope,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
)

from .database import Database


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqliteRuntimeV2MemoryRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def create_memory(
        self,
        *,
        scope: MemoryScope,
        kind: str,
        content: str,
        conversation_id: str,
        workspace_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        run_id: Optional[str] = None,
        source_memory_id: Optional[str] = None,
        source_entry_id: Optional[str] = None,
        memory_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> RuntimeV2MemoryRecord:
        memory_id = memory_id or self._id_factory("v2mem")
        normalized_content = content.strip()
        if not normalized_content:
            raise ConflictError("Memory content cannot be empty")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RuntimeV2MemoryRecord:
            self._ensure_conversation(connection, conversation_id)
            self._validate_scope_dimensions(
                connection,
                scope=scope,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                lane_id=lane_id,
                run_id=run_id,
            )
            if source_memory_id is not None:
                self._get_memory_row(connection, source_memory_id)
            if source_entry_id is not None:
                row = connection.execute(
                    "SELECT conversation_id FROM v2_transcript_entries WHERE id = ?",
                    (source_entry_id,),
                ).fetchone()
                if row is None or row["conversation_id"] != conversation_id:
                    raise InvalidStateError("Memory source entry is invalid")
            connection.execute(
                """
                INSERT INTO v2_runtime_memories(
                    id, scope, kind, content, status, conversation_id,
                    workspace_id, lane_id, run_id, source_memory_id,
                    source_entry_id, created_at, updated_at, expired_at
                )
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    scope.value,
                    kind,
                    normalized_content,
                    conversation_id,
                    workspace_id,
                    lane_id,
                    run_id,
                    source_memory_id,
                    source_entry_id,
                    now,
                    now,
                    expires_at,
                ),
            )
            return self._memory_from_row(
                self._get_memory_row(connection, memory_id)
            )

        return self._write(operation)

    def get_memory(self, memory_id: str) -> RuntimeV2MemoryRecord:
        with self._database.connect() as connection:
            row = self._get_memory_row(connection, memory_id)
        return self._memory_from_row(row)

    def find_active_user_global_memory(
        self,
        *,
        conversation_id: str,
        content: str,
    ) -> Optional[RuntimeV2MemoryRecord]:
        """按内容查找 user_global 作用域的 active 记忆（写入侧幂等去重用）。"""
        with self._database.connect() as connection:
            now = self._clock()
            row = connection.execute(
                """
                SELECT * FROM v2_runtime_memories
                WHERE scope = 'user_global' AND status = 'active'
                  AND (expired_at IS NULL OR expired_at > ?)
                  AND conversation_id = ? AND content = ?
                ORDER BY updated_at, id
                LIMIT 1
                """,
                (now, conversation_id, content),
            ).fetchone()
        return self._memory_from_row(row) if row is not None else None

    def list_active_memories_content(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
    ) -> tuple[tuple[str, str], ...]:
        """返回会话内未过期的 active 记忆 (id, content)，供语义去重比较。"""
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            now = self._clock()
            rows = connection.execute(
                """
                SELECT id, content FROM v2_runtime_memories
                WHERE conversation_id = ? AND status = 'active'
                  AND (expired_at IS NULL OR expired_at > ?)
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (conversation_id, now, limit),
            ).fetchall()
        return tuple((str(row["id"]), str(row["content"])) for row in rows)

    def expire_overdue_memories(self) -> int:
        """把已过期的 active 记忆软删（status='deleted'），返回处理条数。"""
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                UPDATE v2_runtime_memories
                SET status = 'deleted', updated_at = ?
                WHERE status = 'active'
                  AND expired_at IS NOT NULL AND expired_at <= ?
                """,
                (now, now),
            )
            return cursor.rowcount

        return self._write(operation)

    def list_visible_memories(
        self,
        *,
        conversation_id: str,
        workspace_id: Optional[str],
        lane_id: str,
        run_id: Optional[str] = None,
        ancestor_lane_ids: Sequence[str] = (),
    ) -> tuple[RuntimeV2MemoryRecord, ...]:
        visible_lanes = tuple(dict.fromkeys((lane_id, *ancestor_lane_ids)))
        lane_placeholders = ", ".join("?" for _ in visible_lanes)
        parameters: list[Any] = []
        workspace_clause = "workspace_id = ?" if workspace_id else "workspace_id IS NULL"
        if workspace_id:
            parameters.append(workspace_id)
        parameters.append(conversation_id)
        parameters.append(conversation_id)
        parameters.append(lane_id)
        parameters.append(conversation_id)
        parameters.extend(visible_lanes)
        parameters.append(conversation_id)
        run_clause = "run_id = ?" if run_id else "run_id IS NULL"
        if run_id:
            parameters.append(run_id)
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            now = self._clock()
            rows = connection.execute(
                f"""
                SELECT * FROM v2_runtime_memories
                WHERE status = 'active'
                  AND (expired_at IS NULL OR expired_at > ?)
                  AND (
                    scope = 'user_global'
                    OR (scope = 'workspace' AND {workspace_clause})
                    OR (scope = 'conversation_tree' AND conversation_id = ?)
                    OR (
                      scope = 'temporary' AND conversation_id = ? AND lane_id = ?
                    )
                    OR (
                      scope = 'branch' AND conversation_id = ?
                      AND lane_id IN ({lane_placeholders})
                    )
                    OR (
                      scope = 'run_scratch' AND conversation_id = ?
                      AND {run_clause}
                    )
                  )
                ORDER BY CASE scope
                    WHEN 'run_scratch' THEN 1
                    WHEN 'temporary' THEN 2
                    WHEN 'branch' THEN 3
                    WHEN 'conversation_tree' THEN 4
                    WHEN 'workspace' THEN 5
                    WHEN 'user_global' THEN 6
                    ELSE 7
                END, updated_at, id
                """,
                (now, *parameters),
            ).fetchall()
        return tuple(self._memory_from_row(row) for row in rows)

    def create_promotion(
        self,
        *,
        source_memory_id: str,
        target_scope: MemoryScope,
        target_workspace_id: Optional[str] = None,
        target_lane_id: Optional[str] = None,
        promotion_id: Optional[str] = None,
    ) -> RuntimeV2MemoryPromotion:
        promotion_id = promotion_id or self._id_factory("v2mempromo")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RuntimeV2MemoryPromotion:
            source = self._get_memory_row(connection, source_memory_id)
            self._validate_promotion_target(
                connection,
                source=source,
                target_scope=target_scope,
                target_workspace_id=target_workspace_id,
                target_lane_id=target_lane_id,
            )
            pending = connection.execute(
                """
                SELECT 1 FROM v2_memory_promotions
                WHERE source_memory_id = ? AND target_scope = ?
                  AND COALESCE(target_workspace_id, '') = COALESCE(?, '')
                  AND COALESCE(target_lane_id, '') = COALESCE(?, '')
                  AND status = 'pending'
                """,
                (
                    source_memory_id,
                    target_scope.value,
                    target_workspace_id,
                    target_lane_id,
                ),
            ).fetchone()
            if pending is not None:
                raise ConflictError("Memory promotion proposal already exists")
            conflict_memory_id = self._find_conflict_memory_id(
                connection,
                source=source,
                target_scope=target_scope,
                target_workspace_id=target_workspace_id,
                target_lane_id=target_lane_id,
            )
            connection.execute(
                """
                INSERT INTO v2_memory_promotions(
                    id, source_memory_id, target_scope, target_workspace_id,
                    target_lane_id, status, conflict_memory_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    promotion_id,
                    source_memory_id,
                    target_scope.value,
                    target_workspace_id,
                    target_lane_id,
                    conflict_memory_id,
                    now,
                    now,
                ),
            )
            promotion = self._promotion_from_row(
                self._get_promotion_row(connection, promotion_id)
            )
            self._insert_lane_event(
                connection,
                conversation_id=str(source["conversation_id"]),
                lane_id=target_lane_id or source["lane_id"],
                event_type="memory.proposal_created",
                occurred_at=now,
                data={
                    "promotionId": promotion.id,
                    "memoryId": source_memory_id,
                    "targetScope": target_scope.value,
                    "conflictMemoryId": conflict_memory_id,
                },
            )
            return promotion

        return self._write(operation)

    def list_promotions(
        self,
        *,
        source_memory_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        include_resolved: bool = False,
    ) -> tuple[RuntimeV2MemoryPromotion, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if source_memory_id is not None:
            clauses.append("p.source_memory_id = ?")
            parameters.append(source_memory_id)
        if conversation_id is not None:
            clauses.append("m.conversation_id = ?")
            parameters.append(conversation_id)
        if not include_resolved:
            clauses.append("p.status = 'pending'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._database.connect() as connection:
            if conversation_id is not None:
                self._ensure_conversation(connection, conversation_id)
            rows = connection.execute(
                f"""
                SELECT p.* FROM v2_memory_promotions AS p
                JOIN v2_runtime_memories AS m ON m.id = p.source_memory_id
                {where}
                ORDER BY p.created_at, p.id
                """,
                parameters,
            ).fetchall()
        return tuple(self._promotion_from_row(row) for row in rows)

    def get_promotion(self, promotion_id: str) -> RuntimeV2MemoryPromotion:
        with self._database.connect() as connection:
            row = self._get_promotion_row(connection, promotion_id)
        return self._promotion_from_row(row)

    def resolve_promotion(
        self,
        promotion_id: str,
        *,
        accept: bool,
    ) -> tuple[RuntimeV2MemoryPromotion, Optional[RuntimeV2MemoryRecord]]:
        now = self._clock()
        resolved_memory_id: Optional[str] = None

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[RuntimeV2MemoryPromotion, Optional[RuntimeV2MemoryRecord]]:
            row = self._get_promotion_row(connection, promotion_id)
            promotion = self._promotion_from_row(row)
            if promotion.status is not MemoryPromotionStatus.PENDING:
                raise InvalidStateError("Only pending memory promotions can be resolved")
            source = self._get_memory_row(connection, promotion.source_memory_id)
            resolved_memory: Optional[RuntimeV2MemoryRecord] = None
            conflict_memory_id = promotion.conflict_memory_id
            if accept:
                self._validate_promotion_target(
                    connection,
                    source=source,
                    target_scope=promotion.target_scope,
                    target_workspace_id=promotion.target_workspace_id,
                    target_lane_id=promotion.target_lane_id,
                )
                if conflict_memory_id is not None:
                    conflict = self._get_memory_row(connection, conflict_memory_id)
                    if conflict["status"] != "active" or conflict["content"] != source["content"]:
                        conflict_memory_id = None
                if conflict_memory_id is None:
                    conflict_memory_id = self._find_conflict_memory_id(
                        connection,
                        source=source,
                        target_scope=promotion.target_scope,
                        target_workspace_id=promotion.target_workspace_id,
                        target_lane_id=promotion.target_lane_id,
                    )
                if conflict_memory_id is None:
                    resolved_memory_id = self._id_factory("v2mem")
                    connection.execute(
                        """
                        INSERT INTO v2_runtime_memories(
                            id, scope, kind, content, status, conversation_id,
                            workspace_id, lane_id, run_id, source_memory_id,
                            source_entry_id, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?, ?, ?, ?)
                        """,
                        (
                            resolved_memory_id,
                            promotion.target_scope.value,
                            source["kind"],
                            source["content"],
                            source["conversation_id"],
                            promotion.target_workspace_id,
                            promotion.target_lane_id,
                            promotion.source_memory_id,
                            source["source_entry_id"],
                            now,
                            now,
                        ),
                    )
                else:
                    resolved_memory_id = conflict_memory_id
                resolved_memory = self._memory_from_row(
                    self._get_memory_row(connection, resolved_memory_id)
                )
            connection.execute(
                """
                UPDATE v2_memory_promotions
                SET status = ?, resolved_memory_id = ?,
                    conflict_memory_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    MemoryPromotionStatus.ACCEPTED.value
                    if accept
                    else MemoryPromotionStatus.REJECTED.value,
                    resolved_memory_id,
                    conflict_memory_id,
                    now,
                    now,
                    promotion_id,
                ),
            )
            promotion = self._promotion_from_row(
                self._get_promotion_row(connection, promotion_id)
            )
            self._insert_lane_event(
                connection,
                conversation_id=str(source["conversation_id"]),
                lane_id=promotion.target_lane_id or source["lane_id"],
                event_type="memory.scope_changed",
                occurred_at=now,
                data={
                    "promotionId": promotion.id,
                    "memoryId": promotion.source_memory_id,
                    "resolvedMemoryId": resolved_memory_id,
                    "conflictMemoryId": conflict_memory_id,
                    "reusedExistingMemory": (
                        accept and conflict_memory_id is not None
                    ),
                    "targetScope": promotion.target_scope.value,
                    "accepted": accept,
                },
            )
            return promotion, resolved_memory

        return self._write(operation)

    def _validate_promotion_target(
        self,
        connection: sqlite3.Connection,
        *,
        source: sqlite3.Row,
        target_scope: MemoryScope,
        target_workspace_id: Optional[str],
        target_lane_id: Optional[str],
    ) -> None:
        if target_scope not in {
            MemoryScope.USER_GLOBAL,
            MemoryScope.WORKSPACE,
            MemoryScope.CONVERSATION_TREE,
            MemoryScope.BRANCH,
        }:
            raise InvalidStateError("Invalid memory promotion target scope")
        if _scope_rank(target_scope) >= _scope_rank(MemoryScope(source["scope"])):
            raise InvalidStateError("Memory can only be promoted to a broader scope")
        if target_scope is MemoryScope.WORKSPACE:
            if not target_workspace_id:
                raise InvalidStateError("Workspace memory promotion requires a workspace")
            workspace = connection.execute(
                "SELECT 1 FROM workspaces WHERE id = ?",
                (target_workspace_id,),
            ).fetchone()
            if workspace is None:
                raise NotFoundError(f"Workspace not found: {target_workspace_id}")
        if target_scope is MemoryScope.BRANCH:
            if not target_lane_id:
                raise InvalidStateError("Branch memory promotion requires a lane")
            lane = connection.execute(
                "SELECT * FROM v2_lanes WHERE id = ?",
                (target_lane_id,),
            ).fetchone()
            if lane is None:
                raise NotFoundError(f"Lane not found: {target_lane_id}")
            if lane["conversation_id"] != source["conversation_id"]:
                raise InvalidStateError("Promotion target lane does not belong to conversation")
            if lane["kind"] not in ("main", "persistent_branch"):
                raise InvalidStateError("Promotion target lane must be a persistent lane")

    @staticmethod
    def _find_conflict_memory_id(
        connection: sqlite3.Connection,
        *,
        source: sqlite3.Row,
        target_scope: MemoryScope,
        target_workspace_id: Optional[str],
        target_lane_id: Optional[str],
    ) -> Optional[str]:
        clauses = ["scope = ?", "status = 'active'", "content = ?"]
        parameters: list[Any] = [target_scope.value, source["content"]]
        if target_scope is MemoryScope.WORKSPACE:
            clauses.append("workspace_id = ?")
            parameters.append(target_workspace_id)
        elif target_scope is MemoryScope.CONVERSATION_TREE:
            clauses.append("conversation_id = ?")
            parameters.append(source["conversation_id"])
        elif target_scope is MemoryScope.BRANCH:
            clauses.extend(
                ["conversation_id = ?", "lane_id = ?"]
            )
            parameters.extend(
                [source["conversation_id"], target_lane_id]
            )
        row = connection.execute(
            f"""
            SELECT id FROM v2_runtime_memories
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at, id
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        return None if row is None else str(row["id"])

    def _validate_scope_dimensions(
        self,
        connection: sqlite3.Connection,
        *,
        scope: MemoryScope,
        conversation_id: str,
        workspace_id: Optional[str],
        lane_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        if scope is MemoryScope.USER_GLOBAL:
            if workspace_id or lane_id or run_id:
                raise InvalidStateError("User global memory cannot have narrower dimensions")
        elif scope is MemoryScope.WORKSPACE:
            if not workspace_id or lane_id or run_id:
                raise InvalidStateError("Workspace memory requires only a workspace dimension")
            workspace = connection.execute(
                "SELECT 1 FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise NotFoundError(f"Workspace not found: {workspace_id}")
        elif scope is MemoryScope.CONVERSATION_TREE:
            if workspace_id or lane_id or run_id:
                raise InvalidStateError(
                    "Conversation tree memory cannot have narrower dimensions"
                )
        elif scope in {MemoryScope.BRANCH, MemoryScope.TEMPORARY}:
            if workspace_id or run_id or not lane_id:
                raise InvalidStateError("Lane memory requires only a lane dimension")
            lane = connection.execute(
                "SELECT * FROM v2_lanes WHERE id = ?",
                (lane_id,),
            ).fetchone()
            if lane is None:
                raise NotFoundError(f"Lane not found: {lane_id}")
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == "archived" or lane["status"] == "archived":
                raise InvalidStateError("Archived lanes cannot store lane memories")
            if scope is MemoryScope.TEMPORARY:
                expected_kinds = ("temporary",)
            else:
                expected_kinds = ("main", "persistent_branch")
            if lane["kind"] not in expected_kinds:
                raise InvalidStateError("Lane kind does not match memory scope")
        elif scope is MemoryScope.RUN_SCRATCH:
            if workspace_id or not run_id:
                raise InvalidStateError("Run scratch memory requires only a run dimension")
            run = connection.execute(
                "SELECT * FROM v2_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"Run not found: {run_id}")
            if run["conversation_id"] != conversation_id or run["lane_id"] != lane_id:
                raise InvalidStateError("Run does not match memory dimensions")

    @staticmethod
    def _ensure_conversation(
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Conversation not found: {conversation_id}")

    @staticmethod
    def _get_memory_row(
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_runtime_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Runtime v2 memory not found: {memory_id}")
        return row

    @staticmethod
    def _get_promotion_row(
        connection: sqlite3.Connection,
        promotion_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_memory_promotions WHERE id = ?",
            (promotion_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Runtime v2 memory promotion not found: {promotion_id}")
        return row

    def _insert_lane_event(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        lane_id: Optional[str],
        event_type: str,
        occurred_at: str,
        data: Mapping[str, Any],
    ) -> None:
        if lane_id is None:
            raise InvalidStateError("Memory events require a lane")
        event_id = self._id_factory("lane_event")
        event_seq = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1
            FROM v2_lane_events WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO v2_lane_events(
                id, conversation_id, lane_id, event_seq, event_type,
                occurred_at, data_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                conversation_id,
                lane_id,
                event_seq,
                event_type,
                occurred_at,
                _dump(data),
            ),
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> RuntimeV2MemoryRecord:
        return RuntimeV2MemoryRecord(
            id=row["id"],
            scope=MemoryScope(row["scope"]),
            kind=row["kind"],
            content=row["content"],
            status=row["status"],
            conversation_id=row["conversation_id"],
            workspace_id=row["workspace_id"],
            lane_id=row["lane_id"],
            run_id=row["run_id"],
            source_memory_id=row["source_memory_id"],
            source_entry_id=row["source_entry_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expired_at=row["expired_at"],
        )

    @staticmethod
    def _promotion_from_row(row: sqlite3.Row) -> RuntimeV2MemoryPromotion:
        return RuntimeV2MemoryPromotion(
            id=row["id"],
            source_memory_id=row["source_memory_id"],
            target_scope=MemoryScope(row["target_scope"]),
            target_workspace_id=row["target_workspace_id"],
            target_lane_id=row["target_lane_id"],
            status=MemoryPromotionStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_memory_id=row["resolved_memory_id"],
            conflict_memory_id=row["conflict_memory_id"],
            resolved_at=row["resolved_at"],
        )

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        try:
            with self._database.transaction() as connection:
                return operation(connection)
        except sqlite3.IntegrityError as error:
            raise ConflictError(str(error)) from error


def _scope_rank(scope: MemoryScope) -> int:
    return {
        MemoryScope.USER_GLOBAL: 0,
        MemoryScope.WORKSPACE: 1,
        MemoryScope.CONVERSATION_TREE: 2,
        MemoryScope.BRANCH: 3,
        MemoryScope.TEMPORARY: 4,
        MemoryScope.RUN_SCRATCH: 5,
    }[scope]
