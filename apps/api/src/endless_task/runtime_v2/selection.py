from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from endless_task.domain.repositories import ConflictError, NotFoundError

if TYPE_CHECKING:
    from endless_task.storage import Database, SqliteRuntimeV2Repository


@dataclass(frozen=True)
class RuntimeV2GlobalRuntimeStatus:
    default_runtime: str
    rollback_forced: bool
    migration_state: str
    conversation_count: int
    mapped_conversation_count: int
    conversation_tree_count: int
    pending_migration_count: int
    rollback_reconciliation_count: int = 0


@dataclass(frozen=True)
class RuntimeV2ConversationRuntimeStatus:
    conversation_id: str
    tree_conversation_id: str
    default_runtime: str
    rollback_forced: bool
    override_runtime: Optional[str]
    effective_runtime: str
    can_use_v2: bool
    requires_migration: bool
    v1_read_only: bool
    rollback_reconciliation_required: bool
    reason: str


class RuntimeV2RuntimeSelectionService:
    """Resolves the global runtime default and per-conversation override.

    `v1` is a forced rollback mode. `v2` is the default mode and still allows a
    conversation-level v1 override.
    """

    def __init__(
        self,
        *,
        database: Database,
        repository: SqliteRuntimeV2Repository,
        default_runtime: str,
        rollback_forced: bool = False,
    ) -> None:
        self._database = database
        self._repository = repository
        self._default_runtime = self._validate_runtime(default_runtime)
        self._rollback_forced = rollback_forced

    def global_status(self) -> RuntimeV2GlobalRuntimeStatus:
        with self._database.connect() as connection:
            migration_row = connection.execute(
                "SELECT migrated_at FROM v2_migration_state WHERE migration_name = ?",
                ("v1_to_runtime_v2",),
            ).fetchone()
            conversation_count = int(
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            )
            mapping_row = connection.execute(
                """
                SELECT COUNT(*) AS mapped,
                       COUNT(DISTINCT tree_conversation_id) AS trees
                FROM v2_migration_conversation_mappings
                """
            ).fetchone()
            pending_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM conversations AS c
                WHERE EXISTS (
                    SELECT 1 FROM turns AS t WHERE t.conversation_id = c.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM v2_migration_conversation_mappings AS m
                    WHERE m.source_conversation_id = c.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM v2_conversation_pointers AS p
                    WHERE p.conversation_id = c.id
                )
                """
            ).fetchone()
            reconciliation_count = 0
            if migration_row is not None:
                migrated_at = str(migration_row["migrated_at"])
                tree_rows = connection.execute(
                    """
                    SELECT DISTINCT tree_conversation_id
                    FROM v2_migration_conversation_mappings
                    """
                ).fetchall()
                reconciliation_count = sum(
                    1
                    for tree_row in tree_rows
                    if self._rollback_reconciliation_required(
                        connection,
                        tree_conversation_id=str(tree_row["tree_conversation_id"]),
                        migrated_at=migrated_at,
                    )
                )
        return RuntimeV2GlobalRuntimeStatus(
            default_runtime=self._default_runtime,
            rollback_forced=self._rollback_forced,
            migration_state=(
                "migrated" if migration_row is not None else "not_migrated"
            ),
            conversation_count=conversation_count,
            mapped_conversation_count=int(mapping_row["mapped"]),
            conversation_tree_count=int(mapping_row["trees"]),
            pending_migration_count=int(pending_row["count"]),
            rollback_reconciliation_count=reconciliation_count,
        )

    def describe(self, conversation_id: str) -> RuntimeV2ConversationRuntimeStatus:
        with self._database.connect() as connection:
            conversation = connection.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError(f"Conversation not found: {conversation_id}")
            mapping = connection.execute(
                """
                SELECT tree_conversation_id
                FROM v2_migration_conversation_mappings
                WHERE source_conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            migration_row = connection.execute(
                """
                SELECT migrated_at
                FROM v2_migration_state
                WHERE migration_name = ?
                """,
                ("v1_to_runtime_v2",),
            ).fetchone()
            tree_conversation_id = (
                str(mapping["tree_conversation_id"])
                if mapping is not None
                else conversation_id
            )
            turn_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM turns WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            rollback_reconciliation_required = (
                mapping is not None
                and migration_row is not None
                and self._rollback_reconciliation_required(
                    connection,
                    tree_conversation_id=tree_conversation_id,
                    migrated_at=str(migration_row["migrated_at"]),
                )
            )
        pointer = self._repository.get_conversation_pointer(tree_conversation_id)
        override = self._repository.get_runtime_override(conversation_id)
        can_use_v2 = (
            mapping is not None or pointer is not None or turn_count == 0
        ) and not rollback_reconciliation_required

        if self._rollback_forced:
            effective_runtime = "v1"
            reason = "global_v1_rollback"
        elif rollback_reconciliation_required:
            effective_runtime = "v1"
            reason = "rollback_reconciliation_required"
        elif override is not None:
            effective_runtime = override
            reason = "conversation_override"
        elif self._default_runtime == "v2" and can_use_v2:
            effective_runtime = "v2"
            reason = "global_default"
        else:
            effective_runtime = "v1"
            reason = "migration_required"

        return RuntimeV2ConversationRuntimeStatus(
            conversation_id=conversation_id,
            tree_conversation_id=tree_conversation_id,
            default_runtime=self._default_runtime,
            rollback_forced=self._rollback_forced,
            override_runtime=override,
            effective_runtime=effective_runtime,
            can_use_v2=can_use_v2,
            requires_migration=not can_use_v2,
            v1_read_only=mapping is not None,
            rollback_reconciliation_required=rollback_reconciliation_required,
            reason=reason,
        )

    def set_override(
        self,
        conversation_id: str,
        *,
        runtime: str,
    ) -> RuntimeV2ConversationRuntimeStatus:
        normalized = self._validate_runtime(runtime)
        status = self.describe(conversation_id)
        if normalized == "v2" and not status.can_use_v2:
            raise ConflictError(
                "Conversation must be migrated before selecting runtime v2"
            )
        if (
            normalized == "v1"
            and status.v1_read_only
            and not self._rollback_forced
        ):
            raise ConflictError(
                "Migrated v1 conversations are read-only; use global rollback to write v1"
            )
        self._repository.set_runtime_override(
            conversation_id=conversation_id,
            runtime=normalized,
        )
        return self.describe(conversation_id)

    def ensure_v1_write_allowed(self, conversation_id: str) -> None:
        status = self.describe(conversation_id)
        if status.v1_read_only and not self._rollback_forced:
            raise ConflictError(
                "Migrated v1 conversations are read-only; use global rollback to write v1"
            )

    @staticmethod
    def _validate_runtime(runtime: str) -> str:
        normalized = runtime.strip().lower()
        if normalized not in {"v1", "v2"}:
            raise ConflictError("Runtime must be v1 or v2")
        return normalized

    @staticmethod
    def _rollback_reconciliation_required(
        connection,
        *,
        tree_conversation_id: str,
        migrated_at: str,
    ) -> bool:
        post_migration_turn = connection.execute(
            """
            SELECT 1
            FROM turns AS turn
            JOIN v2_migration_conversation_mappings AS mapping
              ON mapping.source_conversation_id = turn.conversation_id
            WHERE mapping.tree_conversation_id = ?
              AND turn.created_at > ?
            LIMIT 1
            """,
            (tree_conversation_id, migrated_at),
        ).fetchone()
        if post_migration_turn is not None:
            return True

        promoted_after_migration = connection.execute(
            """
            SELECT 1
            FROM conversations AS conversation
            JOIN v2_migration_conversation_mappings AS mapping
              ON mapping.source_conversation_id = conversation.id
            WHERE mapping.tree_conversation_id = ?
              AND conversation.promoted_at > ?
            LIMIT 1
            """,
            (tree_conversation_id, migrated_at),
        ).fetchone()
        if promoted_after_migration is not None:
            return True

        unmapped_descendant = connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM conversations WHERE id = ?
                UNION ALL
                SELECT child.id
                FROM conversations AS child
                JOIN descendants AS parent
                  ON child.parent_conversation_id = parent.id
            )
            SELECT descendants.id
            FROM descendants
            LEFT JOIN v2_migration_conversation_mappings AS mapping
              ON mapping.source_conversation_id = descendants.id
            WHERE mapping.source_conversation_id IS NULL
            LIMIT 1
            """,
            (tree_conversation_id,),
        ).fetchone()
        return unmapped_descendant is not None
