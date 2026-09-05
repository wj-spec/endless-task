"""Runtime selection service — v2 sole runtime (14-runtime-single-mode, A2).

旧版本含 v1 override/rollback/migration-required 分支（v1 引擎已删，只剩控制面）。
A2 将该服务收敛为 **v2 唯一**：

- ``default_runtime`` 恒 ``v2``、``rollback_forced`` 恒 False（构造参数删除）；
- per-conversation override 移除（``v2_conversation_runtime_overrides`` 表由
  migration 058 删除）；
- describe 恒 ``effective_runtime=v2 / reason=v2_sole_runtime``，不再计算
  requires_migration / v1_read_only / rollback_reconciliation；
- global_status 保留会话/迁移计数（B 阶段删表前仍具诊断价值），
  rollback_reconciliation_count 恒 0；
- 回滚对账判定 ``rollback_reconciliation_required`` 移至 ``migration.py``
  （B 阶段删除 migration 机制时一并移除），本模块不再提供 v1 能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

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
    """Resolves runtime status — v2 is the only runtime (14 A2).

    Kept as a status/diagnostic service for the runtime endpoints and the
    conversation snapshot; it can no longer select or report v1.
    """

    def __init__(
        self,
        *,
        database: Database,
        repository: SqliteRuntimeV2Repository,
    ) -> None:
        self._database = database
        self._repository = repository

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
        return RuntimeV2GlobalRuntimeStatus(
            default_runtime="v2",
            rollback_forced=False,
            migration_state=(
                "migrated" if migration_row is not None else "not_migrated"
            ),
            conversation_count=conversation_count,
            mapped_conversation_count=int(mapping_row["mapped"]),
            conversation_tree_count=int(mapping_row["trees"]),
            pending_migration_count=int(pending_row["count"]),
            rollback_reconciliation_count=0,
        )

    def describe(self, conversation_id: str) -> RuntimeV2ConversationRuntimeStatus:
        with self._database.connect() as connection:
            conversation = connection.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                from endless_task.domain.repositories import NotFoundError

                raise NotFoundError(f"Conversation not found: {conversation_id}")
            mapping = connection.execute(
                """
                SELECT tree_conversation_id
                FROM v2_migration_conversation_mappings
                WHERE source_conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        tree_conversation_id = (
            str(mapping["tree_conversation_id"])
            if mapping is not None
            else conversation_id
        )
        return RuntimeV2ConversationRuntimeStatus(
            conversation_id=conversation_id,
            tree_conversation_id=tree_conversation_id,
            default_runtime="v2",
            rollback_forced=False,
            override_runtime=None,
            effective_runtime="v2",
            can_use_v2=True,
            requires_migration=False,
            v1_read_only=False,
            rollback_reconciliation_required=False,
            reason="v2_sole_runtime",
        )


__all__ = [
    "RuntimeV2ConversationRuntimeStatus",
    "RuntimeV2GlobalRuntimeStatus",
    "RuntimeV2RuntimeSelectionService",
]
