from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from endless_task.domain.repositories import ConflictError

from endless_task.storage.database import Database

from .selection import RuntimeV2RuntimeSelectionService


MIGRATION_NAME = "v1_to_runtime_v2"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RuntimeV2MigrationReport:
    conversation_count: int
    lane_count: int
    entry_count: int
    run_count: int
    model_turn_count: int
    tool_execution_count: int
    runtime_event_count: int
    context_compaction_count: int
    conversation_tree_count: int = 0
    branch_conversation_count: int = 0
    conversation_mapping_count: int = 0
    legacy_temporary_lane_repair_count: int = 0
    warnings: tuple[str, ...] = ()
    already_migrated: bool = False


@dataclass(frozen=True)
class RuntimeV2MigrationAuditReport:
    migration_state: str
    conversation_count: int
    mapped_conversation_count: int
    conversation_tree_count: int
    branch_conversation_count: int
    pending_migration_count: int
    errors: tuple[str, ...] = ()
    rollback_reconciliation_count: int = 0
    legacy_temporary_lane_repair_count: int = 0
    pending_legacy_temporary_lane_repair_count: int = 0

    @property
    def passed(self) -> bool:
        return not self.errors


class RuntimeV2MigrationService:
    """Migrates v1 chat/runtime rows into the v2 Agent Runtime model.

    The migration is transactional and idempotent. v1 tables remain untouched;
    completion is recorded in `v2_migration_state`.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def migrate(self) -> RuntimeV2MigrationReport:
        existing = self._load_state()
        if existing is not None:
            return self._repair_existing_migration(existing)

        warnings: list[str] = []
        with self._database.transaction() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            existing = connection.execute(
                "SELECT report_json FROM v2_migration_state WHERE migration_name = ?",
                (MIGRATION_NAME,),
            ).fetchone()
            if existing is not None:
                return self._repair_existing_migration_report(
                    connection,
                    self._report_from_json(existing["report_json"], already=True),
                )

            if connection.execute("SELECT 1 FROM v2_lanes LIMIT 1").fetchone() is not None:
                raise ConflictError("v2 data already exists without migration state")

            conversation_rows = connection.execute(
                "SELECT * FROM conversations ORDER BY created_at, id"
            ).fetchall()
            self._migrate_conversations(connection, conversation_rows, warnings)

            self._migrate_runtime_events(connection)
            report = self._build_report(connection, warnings)
            connection.execute(
                """
                INSERT INTO v2_migration_state(
                    migration_name, migrated_at, report_json
                )
                VALUES (?, ?, ?)
                """,
                (MIGRATION_NAME, _utc_now(), _dump(asdict(report))),
            )
            return report

    def _repair_existing_migration(
        self,
        existing: RuntimeV2MigrationReport,
    ) -> RuntimeV2MigrationReport:
        with self._database.transaction() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            state_row = connection.execute(
                "SELECT report_json FROM v2_migration_state WHERE migration_name = ?",
                (MIGRATION_NAME,),
            ).fetchone()
            if state_row is None:
                return existing
            return self._repair_existing_migration_report(
                connection,
                self._report_from_json(state_row["report_json"], already=True),
            )

    def _repair_existing_migration_report(
        self,
        connection: sqlite3.Connection,
        existing: RuntimeV2MigrationReport,
    ) -> RuntimeV2MigrationReport:
        warnings = list(existing.warnings)
        repair_count = self._repair_legacy_temporary_lanes(connection, warnings)
        if repair_count == 0:
            return existing

        report = replace(
            self._build_report(connection, warnings),
            legacy_temporary_lane_repair_count=(
                existing.legacy_temporary_lane_repair_count + repair_count
            ),
        )
        connection.execute(
            """
            UPDATE v2_migration_state
            SET report_json = ?
            WHERE migration_name = ?
            """,
            (_dump(asdict(report)), MIGRATION_NAME),
        )
        return replace(report, already_migrated=True)

    def _repair_legacy_temporary_lanes(
        self,
        connection: sqlite3.Connection,
        warnings: list[str],
    ) -> int:
        rows = self._legacy_temporary_lane_repair_rows(connection)
        repaired = 0
        for row in rows:
            source_conversation_id = str(row["source_conversation_id"])
            old_tree_conversation_id = str(row["tree_conversation_id"])
            lane_id = str(row["lane_id"])
            source_leaf_entry_id = row["base_entry_id"]
            source_lane_id = self._legacy_source_lane_id(connection, row)
            source_context_ids: tuple[str, ...] = ()
            copied_entry_ids: tuple[str, ...] = ()
            copied_leaf_entry_id: Optional[str] = None

            if source_leaf_entry_id is not None:
                source_context_ids = tuple(
                    context_row["id"]
                    for context_row in self._v2_entry_context_rows(
                        connection,
                        str(source_leaf_entry_id),
                        warnings,
                    )
                )
                existing_entry_count = self._lane_entry_count(connection, lane_id)
                if source_context_ids and existing_entry_count:
                    connection.execute(
                        """
                        UPDATE v2_transcript_entries
                        SET seq = seq + 1000000
                        WHERE lane_id = ?
                        """,
                        (lane_id,),
                    )
                (
                    copied_leaf_entry_id,
                    _source_ids,
                    copied_entry_ids,
                ) = self._copy_context_snapshot(
                    connection,
                    target_conversation_id=source_conversation_id,
                    lane_id=lane_id,
                    source_leaf_entry_id=str(source_leaf_entry_id),
                    warnings=warnings,
                )
                if copied_entry_ids and existing_entry_count:
                    connection.execute(
                        """
                        UPDATE v2_transcript_entries
                        SET seq = seq - 1000000 + ?
                        WHERE lane_id = ? AND seq >= 1000000
                        """,
                        (len(copied_entry_ids), lane_id),
                    )

            connection.execute(
                """
                UPDATE v2_transcript_entries
                SET parent_id = ?
                WHERE lane_id = ?
                  AND parent_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM v2_transcript_entries AS parent
                      WHERE parent.id = v2_transcript_entries.parent_id
                        AND parent.lane_id = ?
                  )
                """,
                (copied_leaf_entry_id, lane_id, lane_id),
            )
            connection.execute(
                """
                UPDATE v2_transcript_entries
                SET conversation_id = ?
                WHERE lane_id = ?
                """,
                (source_conversation_id, lane_id),
            )
            connection.execute(
                "UPDATE v2_runs SET conversation_id = ? WHERE lane_id = ?",
                (source_conversation_id, lane_id),
            )
            connection.execute(
                """
                UPDATE v2_context_compactions
                SET conversation_id = ?
                WHERE lane_id = ?
                """,
                (source_conversation_id, lane_id),
            )
            if copied_leaf_entry_id is not None:
                connection.execute(
                    """
                    UPDATE v2_context_compactions
                    SET base_entry_id = ?
                    WHERE lane_id = ?
                      AND base_entry_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM v2_transcript_entries AS entry
                          WHERE entry.id = v2_context_compactions.base_entry_id
                            AND entry.lane_id = ?
                      )
                    """,
                    (copied_leaf_entry_id, lane_id, lane_id),
                )

            metadata = self._json_object(row["metadata_json"])
            metadata.update(
                {
                    "sourceConversationId": source_conversation_id,
                    "treeConversationId": source_conversation_id,
                    "sourceParentConversationId": row["source_parent_conversation_id"],
                    "sourceLaneId": source_lane_id,
                    "sourceLeafEntryId": source_leaf_entry_id,
                    "legacyTemporaryLaneRepair": True,
                    "legacyTreeConversationId": old_tree_conversation_id,
                    "independentTreeReason": "legacy_temporary_lane_repair",
                }
            )
            metadata.pop("baseEntryId", None)
            source_excerpt = self._entry_excerpt_for_entry(
                connection,
                str(source_leaf_entry_id) if source_leaf_entry_id is not None else None,
            )
            leaf_entry_id = row["leaf_entry_id"]
            if leaf_entry_id is None or leaf_entry_id == source_leaf_entry_id:
                leaf_entry_id = copied_leaf_entry_id
            connection.execute(
                """
                UPDATE v2_lanes
                SET conversation_id = ?, kind = 'temporary', base_entry_id = NULL,
                    leaf_entry_id = ?, metadata_json = ?, status = 'active',
                    archived_at = NULL, summary = ?, source_lane_id = ?,
                    created_from_entry_id = ?
                WHERE id = ?
                """,
                (
                    source_conversation_id,
                    leaf_entry_id,
                    _dump(metadata),
                    source_excerpt,
                    source_lane_id,
                    source_leaf_entry_id,
                    lane_id,
                ),
            )

            active_run_id = self._active_run_id_for_lane(connection, lane_id)
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
                    source_conversation_id,
                    lane_id,
                    active_run_id,
                    active_run_id,
                    row["conversation_updated_at"],
                ),
            )
            self._restore_pointer_after_legacy_temporary_repair(
                connection,
                old_tree_conversation_id=old_tree_conversation_id,
                moved_lane_id=lane_id,
                updated_at=row["conversation_updated_at"],
            )
            self._upsert_temporary_conversation_repair_record(
                connection,
                row=row,
                source_lane_id=source_lane_id,
                source_leaf_entry_id=(
                    str(source_leaf_entry_id) if source_leaf_entry_id is not None else None
                ),
                source_context_ids=source_context_ids,
                copied_entry_ids=copied_entry_ids,
            )
            self._repair_legacy_temporary_lane_event(
                connection,
                row=row,
                source_lane_id=source_lane_id,
                source_leaf_entry_id=(
                    str(source_leaf_entry_id) if source_leaf_entry_id is not None else None
                ),
                metadata=metadata,
            )
            connection.execute(
                """
                UPDATE v2_migration_conversation_mappings
                SET tree_conversation_id = ?, lane_kind = 'temporary'
                WHERE source_conversation_id = ?
                """,
                (source_conversation_id, source_conversation_id),
            )
            repaired += 1

        if repaired:
            warnings.append(
                f"Repaired {repaired} legacy temporary lane(s) into independent temporary conversations"
            )
        return repaired

    def _legacy_temporary_lane_repair_rows(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[sqlite3.Row, ...]:
        return tuple(
            connection.execute(
                """
                SELECT mapping.source_conversation_id,
                       mapping.tree_conversation_id,
                       mapping.lane_id,
                       mapping.source_parent_conversation_id,
                       mapping.source_fork_turn_id,
                       conversation.created_at AS conversation_created_at,
                       conversation.updated_at AS conversation_updated_at,
                       lane.conversation_id AS lane_conversation_id,
                       lane.base_entry_id,
                       lane.leaf_entry_id,
                       lane.created_at AS lane_created_at,
                       lane.metadata_json
                FROM v2_migration_conversation_mappings AS mapping
                JOIN conversations AS conversation
                  ON conversation.id = mapping.source_conversation_id
                JOIN v2_lanes AS lane ON lane.id = mapping.lane_id
                WHERE conversation.kind = 'ephemeral'
                  AND conversation.promoted_at IS NULL
                  AND lane.kind = 'temporary'
                  AND (
                      mapping.tree_conversation_id <> mapping.source_conversation_id
                      OR lane.conversation_id <> mapping.source_conversation_id
                  )
                ORDER BY conversation.created_at, conversation.id
                """
            ).fetchall()
        )

    def _legacy_source_lane_id(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Optional[str]:
        metadata = self._json_object(row["metadata_json"])
        source_lane_id = metadata.get("sourceLaneId")
        if isinstance(source_lane_id, str):
            return source_lane_id
        parent_id = row["source_parent_conversation_id"]
        if parent_id is None:
            return None
        parent_mapping = connection.execute(
            """
            SELECT lane_id
            FROM v2_migration_conversation_mappings
            WHERE source_conversation_id = ?
            """,
            (parent_id,),
        ).fetchone()
        if parent_mapping is not None:
            return str(parent_mapping["lane_id"])
        return f"v2_lane_{parent_id}"

    @staticmethod
    def _lane_entry_count(connection: sqlite3.Connection, lane_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM v2_transcript_entries WHERE lane_id = ?",
                (lane_id,),
            ).fetchone()[0]
        )

    def _entry_excerpt_for_entry(
        self,
        connection: sqlite3.Connection,
        entry_id: Optional[str],
    ) -> Optional[str]:
        if entry_id is None:
            return None
        row = connection.execute(
            "SELECT payload_json FROM v2_transcript_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        payload = self._json_object(row["payload_json"])
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        normalized = " ".join(content.split())
        if not normalized:
            return None
        return normalized if len(normalized) <= 80 else normalized[:79] + "…"

    def _active_run_id_for_lane(
        self,
        connection: sqlite3.Connection,
        lane_id: str,
    ) -> Optional[str]:
        row = connection.execute(
            """
            SELECT run.id
            FROM v2_runs AS run
            JOIN v2_transcript_entries AS trigger_entry
              ON trigger_entry.id = run.trigger_entry_id
            WHERE run.lane_id = ? AND run.is_active_variant = 1
            ORDER BY trigger_entry.seq DESC, run.created_at DESC, run.id DESC
            LIMIT 1
            """,
            (lane_id,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def _restore_pointer_after_legacy_temporary_repair(
        self,
        connection: sqlite3.Connection,
        *,
        old_tree_conversation_id: str,
        moved_lane_id: str,
        updated_at: str,
    ) -> None:
        pointer = connection.execute(
            """
            SELECT active_lane_id
            FROM v2_conversation_pointers
            WHERE conversation_id = ?
            """,
            (old_tree_conversation_id,),
        ).fetchone()
        if pointer is None or pointer["active_lane_id"] != moved_lane_id:
            return
        main_lane = connection.execute(
            """
            SELECT id
            FROM v2_lanes
            WHERE conversation_id = ? AND kind = 'main'
            ORDER BY created_at, id
            LIMIT 1
            """,
            (old_tree_conversation_id,),
        ).fetchone()
        if main_lane is None:
            return
        active_run_id = self._active_run_id_for_lane(connection, str(main_lane["id"]))
        connection.execute(
            """
            UPDATE v2_conversation_pointers
            SET active_lane_id = ?, active_run_id = ?,
                active_run_variant_id = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                main_lane["id"],
                active_run_id,
                active_run_id,
                updated_at,
                old_tree_conversation_id,
            ),
        )

    def _upsert_temporary_conversation_repair_record(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        source_lane_id: Optional[str],
        source_leaf_entry_id: Optional[str],
        source_context_ids: Sequence[str],
        copied_entry_ids: Sequence[str],
    ) -> None:
        source_base_entry_id: Optional[str] = None
        if source_lane_id is not None:
            source_lane = connection.execute(
                "SELECT base_entry_id FROM v2_lanes WHERE id = ?",
                (source_lane_id,),
            ).fetchone()
            if source_lane is not None:
                source_base_entry_id = source_lane["base_entry_id"]
        connection.execute(
            """
            INSERT INTO v2_temporary_conversations(
                conversation_id, source_conversation_id, source_lane_id,
                source_base_entry_id, source_leaf_entry_id,
                snapshot_entry_ids_json, created_at, promoted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(conversation_id) DO UPDATE SET
                source_conversation_id = excluded.source_conversation_id,
                source_lane_id = excluded.source_lane_id,
                source_base_entry_id = excluded.source_base_entry_id,
                source_leaf_entry_id = excluded.source_leaf_entry_id,
                snapshot_entry_ids_json = excluded.snapshot_entry_ids_json,
                created_at = excluded.created_at
            """,
            (
                row["source_conversation_id"],
                row["source_parent_conversation_id"],
                source_lane_id,
                source_base_entry_id,
                source_leaf_entry_id,
                _dump(
                    {
                        "entryIds": list(source_context_ids),
                        "copiedEntryIds": list(copied_entry_ids),
                    }
                ),
                row["conversation_created_at"],
            ),
        )

    def _repair_legacy_temporary_lane_event(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        source_lane_id: Optional[str],
        source_leaf_entry_id: Optional[str],
        metadata: Mapping[str, Any],
    ) -> None:
        event_id = f"v2_lane_event_{row['source_conversation_id']}"
        event_data = dict(metadata)
        event_data.update(
            {
                "sourceConversationId": row["source_parent_conversation_id"],
                "temporaryConversationId": row["source_conversation_id"],
                "sourceLaneId": source_lane_id,
                "sourceLeafEntryId": source_leaf_entry_id,
                "legacyTemporaryLaneRepair": True,
            }
        )
        event_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) + 1
                FROM v2_lane_events
                WHERE conversation_id = ? AND id <> ?
                """,
                (row["source_conversation_id"], event_id),
            ).fetchone()[0]
        )
        event_row = connection.execute(
            "SELECT id FROM v2_lane_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if event_row is None:
            connection.execute(
                """
                INSERT INTO v2_lane_events(
                    id, conversation_id, lane_id, event_seq, event_type,
                    occurred_at, data_json
                )
                VALUES (?, ?, ?, ?, 'temporary_conversation.created', ?, ?)
                """,
                (
                    event_id,
                    row["source_conversation_id"],
                    row["lane_id"],
                    event_seq,
                    row["conversation_created_at"],
                    _dump(event_data),
                ),
            )
            return
        connection.execute(
            """
            UPDATE v2_lane_events
            SET conversation_id = ?, lane_id = ?, event_seq = ?,
                event_type = 'temporary_conversation.created', data_json = ?
            WHERE id = ?
            """,
            (
                row["source_conversation_id"],
                row["lane_id"],
                event_seq,
                _dump(event_data),
                event_id,
            ),
        )

    def audit(self) -> RuntimeV2MigrationAuditReport:
        with self._database.connect() as connection:
            state_row = connection.execute(
                """
                SELECT migrated_at, report_json
                FROM v2_migration_state
                WHERE migration_name = ?
                """,
                (MIGRATION_NAME,),
            ).fetchone()
            if state_row is None:
                pending = self._pending_migration_count(connection)
                return RuntimeV2MigrationAuditReport(
                    migration_state="not_migrated",
                    conversation_count=self._count(connection, "conversations"),
                    mapped_conversation_count=0,
                    conversation_tree_count=0,
                    branch_conversation_count=0,
                    pending_migration_count=pending,
                    errors=("migration_state_missing",),
                )

            report = self._report_from_json(state_row["report_json"], already=True)
            errors: list[str] = []
            mapped_count = self._count(
                connection, "v2_migration_conversation_mappings"
            )
            tree_count = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT tree_conversation_id)
                    FROM v2_migration_conversation_mappings
                    """
                ).fetchone()[0]
            )
            branch_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM v2_migration_conversation_mappings
                    WHERE source_conversation_id <> tree_conversation_id
                    """
                ).fetchone()[0]
            )
            pending_count = self._pending_migration_count(connection)
            pending_legacy_repair_count = len(
                self._legacy_temporary_lane_repair_rows(connection)
            )

            self._compare_count(errors, "conversation", report.conversation_count, mapped_count)
            self._compare_count(
                errors, "conversation_mapping", report.conversation_mapping_count, mapped_count
            )
            self._compare_count(
                errors, "conversation_tree", report.conversation_tree_count, tree_count
            )
            self._compare_count(
                errors, "branch_conversation", report.branch_conversation_count, branch_count
            )
            self._compare_count(
                errors,
                "lane",
                report.lane_count,
                self._count_by_prefix(connection, "v2_lanes", "id", "v2_lane_"),
            )
            self._compare_count(
                errors,
                "entry",
                report.entry_count,
                self._count_by_prefix(
                    connection, "v2_transcript_entries", "id", "v2_entry_"
                ),
            )
            self._compare_count(
                errors,
                "run",
                report.run_count,
                self._count_by_prefix(connection, "v2_runs", "id", "v2_run_"),
            )
            self._compare_count(
                errors,
                "model_turn",
                report.model_turn_count,
                self._count_by_prefix(
                    connection, "v2_model_turns", "id", "v2_turn_"
                ),
            )
            self._compare_count(
                errors,
                "tool_execution",
                report.tool_execution_count,
                self._count_by_prefix(
                    connection, "v2_tool_executions", "id", "v2_tool_"
                ),
            )
            self._compare_count(
                errors,
                "runtime_event",
                report.runtime_event_count,
                self._count_by_prefix(
                    connection, "v2_runtime_events", "event_id", "v2_event_"
                ),
            )
            self._compare_count(
                errors,
                "context_compaction",
                report.context_compaction_count,
                self._count_by_prefix(
                    connection, "v2_context_compactions", "id", "v2_compaction_"
                ),
            )

            invalid_mapping_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM v2_migration_conversation_mappings AS mapping
                    JOIN v2_lanes AS lane ON lane.id = mapping.lane_id
                    WHERE mapping.v1_read_only <> 1
                       OR lane.conversation_id <> mapping.tree_conversation_id
                    """
                ).fetchone()[0]
            )
            if invalid_mapping_count:
                errors.append(
                    f"invalid_mapping_count={invalid_mapping_count}"
                )

            missing_pointer_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT DISTINCT tree_conversation_id
                        FROM v2_migration_conversation_mappings
                    ) AS tree
                    LEFT JOIN v2_conversation_pointers AS pointer
                      ON pointer.conversation_id = tree.tree_conversation_id
                    WHERE pointer.conversation_id IS NULL
                    """
                ).fetchone()[0]
            )
            if missing_pointer_count:
                errors.append(f"missing_tree_pointer_count={missing_pointer_count}")

            if pending_count:
                errors.append(f"pending_migration_count={pending_count}")
            if pending_legacy_repair_count:
                errors.append(
                    "legacy_temporary_lane_repair_pending_count="
                    f"{pending_legacy_repair_count}"
                )

            migrated_at = str(state_row["migrated_at"])
            reconciliation_count = sum(
                1
                for tree_row in connection.execute(
                    """
                    SELECT DISTINCT tree_conversation_id
                    FROM v2_migration_conversation_mappings
                    """
                ).fetchall()
                if RuntimeV2RuntimeSelectionService._rollback_reconciliation_required(
                    connection,
                    tree_conversation_id=str(tree_row["tree_conversation_id"]),
                    migrated_at=migrated_at,
                )
            )
            if reconciliation_count:
                errors.append(
                    f"rollback_reconciliation_required_count={reconciliation_count}"
                )

            return RuntimeV2MigrationAuditReport(
                migration_state="migrated",
                conversation_count=report.conversation_count,
                mapped_conversation_count=mapped_count,
                conversation_tree_count=tree_count,
                branch_conversation_count=branch_count,
                pending_migration_count=pending_count,
                errors=tuple(errors),
                rollback_reconciliation_count=reconciliation_count,
                legacy_temporary_lane_repair_count=(
                    report.legacy_temporary_lane_repair_count
                ),
                pending_legacy_temporary_lane_repair_count=(
                    pending_legacy_repair_count
                ),
            )

    def _migrate_conversations(
        self,
        connection: sqlite3.Connection,
        conversation_rows: Sequence[sqlite3.Row],
        warnings: list[str],
    ) -> None:
        rows_by_id = {row["id"]: row for row in conversation_rows}
        children: dict[str, list[sqlite3.Row]] = {
            row["id"]: [] for row in conversation_rows
        }
        roots: list[sqlite3.Row] = []
        for row in conversation_rows:
            parent_id = row["parent_conversation_id"]
            if parent_id is None:
                roots.append(row)
                continue
            if parent_id not in rows_by_id:
                warnings.append(
                    f"Conversation {row['id']} has a missing parent and migrates as a tree root"
                )
                roots.append(row)
                continue
            children[parent_id].append(row)

        for root in roots:
            self._migrate_conversation_tree(
                connection,
                root,
                rows_by_id,
                children,
                warnings,
            )

    def _migrate_conversation_tree(
        self,
        connection: sqlite3.Connection,
        root: sqlite3.Row,
        rows_by_id: Mapping[str, sqlite3.Row],
        children: Mapping[str, Sequence[sqlite3.Row]],
        warnings: list[str],
    ) -> None:
        stack = [root]
        migrated: dict[str, dict[str, Optional[str]]] = {}
        mapping_rows: list[tuple[sqlite3.Row, dict[str, Optional[str]]]] = []
        pointer_rows: list[tuple[str, dict[str, Optional[str]], str]] = []

        while stack:
            conversation = stack.pop()
            conversation_id = conversation["id"]
            parent_id = conversation["parent_conversation_id"]
            parent_result = migrated.get(parent_id) if parent_id is not None else None
            if parent_id is not None and parent_result is None:
                warnings.append(
                    f"Conversation {conversation_id} migrates without its parent lane"
                )

            source_lane_id: Optional[str] = None
            source_leaf_entry_id: Optional[str] = None
            if parent_result is not None:
                source_lane_id = parent_result["lane_id"]
                source_leaf_entry_id = self._fork_base_entry_id(
                    connection,
                    conversation,
                    warnings,
                )

            legacy_temporary = self._is_legacy_temporary_conversation(conversation)
            independent_tree = self._migrates_as_independent_tree(
                conversation,
                parent_result,
            )
            if independent_tree:
                tree_conversation_id = conversation_id
                lane_kind = "temporary" if legacy_temporary else "main"
                base_entry_id = None
                snapshot_source_entry_id = source_leaf_entry_id
                emit_lane_event = legacy_temporary
            else:
                tree_conversation_id = str(parent_result["tree_conversation_id"])
                lane_kind = "persistent_branch"
                base_entry_id = source_leaf_entry_id
                snapshot_source_entry_id = None
                emit_lane_event = True

            lane_id = f"v2_lane_{conversation_id}"
            metadata: dict[str, Any] = {
                "sourceConversationId": conversation_id,
                "treeConversationId": tree_conversation_id,
                "kind": lane_kind,
                "legacyConversationKind": conversation["kind"],
                "v1ReadOnly": True,
                "migratedFrom": "v1_conversation",
            }
            if parent_id is not None:
                metadata["sourceParentConversationId"] = parent_id
            if source_lane_id is not None:
                metadata["sourceLaneId"] = source_lane_id
            if base_entry_id is not None:
                metadata["baseEntryId"] = base_entry_id
            if snapshot_source_entry_id is not None:
                metadata["sourceLeafEntryId"] = snapshot_source_entry_id
            if conversation["fork_turn_id"] is not None:
                metadata["sourceForkTurnId"] = conversation["fork_turn_id"]
            if conversation["promoted_at"] is not None:
                metadata["promotedAt"] = conversation["promoted_at"]
            if independent_tree and parent_result is not None:
                metadata["independentTreeReason"] = (
                    "legacy_temporary_conversation"
                    if legacy_temporary
                    else "legacy_promoted_conversation"
                )

            (
                leaf_entry_id,
                active_run_id,
                snapshot_source_entry_ids,
                snapshot_copied_entry_ids,
            ) = self._migrate_conversation(
                connection,
                conversation,
                warnings,
                target_conversation_id=tree_conversation_id,
                lane_id=lane_id,
                lane_kind=lane_kind,
                base_entry_id=base_entry_id,
                metadata=metadata,
                emit_lane_event=emit_lane_event,
                lane_event_type=(
                    "temporary_conversation.created"
                    if independent_tree and legacy_temporary
                    else "branch.created"
                ),
                snapshot_source_entry_id=snapshot_source_entry_id,
            )
            result = {
                "tree_conversation_id": tree_conversation_id,
                "lane_id": lane_id,
                "leaf_entry_id": leaf_entry_id,
                "active_run_id": active_run_id,
                "promoted_at": conversation["promoted_at"],
                "lane_kind": lane_kind,
            }
            migrated[conversation_id] = result
            mapping_rows.append((conversation, result))
            if independent_tree:
                pointer_rows.append((tree_conversation_id, result, conversation["updated_at"]))
            if legacy_temporary:
                self._insert_temporary_conversation_record(
                    connection,
                    conversation=conversation,
                    source_conversation_id=parent_id if parent_result is not None else None,
                    source_lane_id=source_lane_id,
                    source_leaf_entry_id=snapshot_source_entry_id,
                    snapshot_source_entry_ids=snapshot_source_entry_ids,
                    snapshot_copied_entry_ids=snapshot_copied_entry_ids,
                )
            stack.extend(reversed(children.get(conversation_id, ())))

        for conversation, result in mapping_rows:
            lane_row = connection.execute(
                "SELECT kind FROM v2_lanes WHERE id = ?",
                (result["lane_id"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO v2_migration_conversation_mappings(
                    source_conversation_id, tree_conversation_id, lane_id,
                    source_parent_conversation_id, source_fork_turn_id,
                    lane_kind, v1_read_only, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    conversation["id"],
                    result["tree_conversation_id"],
                    result["lane_id"],
                    conversation["parent_conversation_id"],
                    conversation["fork_turn_id"],
                    lane_row["kind"],
                    _utc_now(),
                ),
            )

        for tree_conversation_id, selected, updated_at in pointer_rows:
            connection.execute(
                """
                INSERT INTO v2_conversation_pointers(
                    conversation_id, active_lane_id, active_run_id,
                    active_run_variant_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tree_conversation_id,
                    selected["lane_id"],
                    selected["active_run_id"],
                    selected["active_run_id"],
                    updated_at,
                ),
            )

    @staticmethod
    def _is_legacy_temporary_conversation(conversation: sqlite3.Row) -> bool:
        return conversation["kind"] == "ephemeral" and conversation["promoted_at"] is None

    def _migrates_as_independent_tree(
        self,
        conversation: sqlite3.Row,
        parent_result: Optional[Mapping[str, Optional[str]]],
    ) -> bool:
        if parent_result is None:
            return True
        if self._is_legacy_temporary_conversation(conversation):
            return True
        return conversation["promoted_at"] is not None

    def _migrate_conversation(
        self,
        connection: sqlite3.Connection,
        conversation: sqlite3.Row,
        warnings: list[str],
        *,
        target_conversation_id: str,
        lane_id: str,
        lane_kind: str,
        base_entry_id: Optional[str],
        metadata: dict[str, Any],
        emit_lane_event: bool,
        lane_event_type: str = "branch.created",
        snapshot_source_entry_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], tuple[str, ...], tuple[str, ...]]:
        source_conversation_id = conversation["id"]
        conversation_id = target_conversation_id

        connection.execute(
            """
            INSERT INTO v2_lanes(
                id, conversation_id, kind, base_entry_id, leaf_entry_id,
                created_at, metadata_json
            )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
            (
                lane_id,
                conversation_id,
                lane_kind,
                base_entry_id,
                base_entry_id,
                conversation["created_at"],
                _dump(metadata),
            ),
        )
        if emit_lane_event:
            self._insert_migration_lane_event(
                connection,
                tree_conversation_id=target_conversation_id,
                lane_id=lane_id,
                conversation=conversation,
                metadata=metadata,
                event_type=lane_event_type,
            )

        snapshot_source_entry_ids: tuple[str, ...] = ()
        snapshot_copied_entry_ids: tuple[str, ...] = ()
        current_parent = base_entry_id
        seq = 0
        if snapshot_source_entry_id is not None:
            (
                current_parent,
                snapshot_source_entry_ids,
                snapshot_copied_entry_ids,
            ) = self._copy_context_snapshot(
                connection,
                target_conversation_id=conversation_id,
                lane_id=lane_id,
                source_leaf_entry_id=snapshot_source_entry_id,
                warnings=warnings,
            )
            seq = len(snapshot_copied_entry_ids)
        active_run_id: Optional[str] = None
        turn_rows = connection.execute(
            """
            SELECT * FROM turns
            WHERE conversation_id = ?
            ORDER BY ordinal, id
            """,
            (source_conversation_id,),
        ).fetchall()

        for turn in turn_rows:
            user_message = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (turn["user_message_id"],),
            ).fetchone()
            if user_message is None:
                warnings.append(f"Missing user message for turn {turn['id']}")
                continue

            user_entry_id = f"v2_entry_{user_message['id']}"
            seq += 1
            self._insert_entry(
                connection,
                entry_id=user_entry_id,
                conversation_id=conversation_id,
                parent_id=current_parent,
                lane_id=lane_id,
                seq=seq,
                entry_type="user_message",
                actor="user",
                status="final",
                created_at=user_message["created_at"],
                payload={
                    "content": user_message["content"],
                    "attachments": [],
                    "references": [],
                    "inputKind": "normal",
                },
            )

            variant_rows = connection.execute(
                """
                SELECT * FROM response_variants
                WHERE turn_id = ?
                ORDER BY variant_index, id
                """,
                (turn["id"],),
            ).fetchall()
            if not variant_rows:
                warnings.append(f"Turn {turn['id']} has no response variant")
                current_parent = user_entry_id
                continue

            active_variant_id = turn["active_response_variant_id"]
            if active_variant_id is None:
                active_variant_id = variant_rows[0]["id"]
                warnings.append(f"Turn {turn['id']} has no active response variant")

            active_variant_last_entry = user_entry_id
            for variant in variant_rows:
                run_id = f"v2_run_{variant['id']}"
                is_active = variant["id"] == active_variant_id
                assistant_message = connection.execute(
                    "SELECT * FROM messages WHERE id = ?",
                    (variant["assistant_message_id"],),
                ).fetchone()
                if assistant_message is None:
                    warnings.append(
                        f"Response variant {variant['id']} has no assistant message"
                    )
                    continue

                assistant_entry_id = f"v2_entry_{assistant_message['id']}"
                seq += 1
                self._insert_entry(
                    connection,
                    entry_id=assistant_entry_id,
                    conversation_id=conversation_id,
                    parent_id=user_entry_id,
                    lane_id=lane_id,
                    seq=seq,
                    entry_type="assistant_message",
                    actor="assistant",
                    status="final",
                    created_at=assistant_message["created_at"],
                    payload={
                        "content": assistant_message["content"],
                        "provider": variant["provider"],
                        "model": variant["model"],
                        "finishReason": variant["finish_reason"],
                        "runId": run_id,
                        "usage": {
                            "inputTokens": variant["input_tokens"],
                            "outputTokens": variant["output_tokens"],
                        },
                    },
                    source_run_id=run_id,
                )

                run_status, run_error = self._run_status(
                    variant["status"],
                    variant["error_code"],
                )
                if run_status == "failed" and run_error == "v1_interrupted":
                    warnings.append(
                        f"Response variant {variant['id']} was interrupted before migration"
                    )
                connection.execute(
                    """
                    INSERT INTO v2_runs(
                        id, conversation_id, lane_id, trigger_entry_id,
                        sibling_group_id, assistant_entry_id, is_active_variant,
                        status, created_at, started_at, finished_at,
                        error_code, safe_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        conversation_id,
                        lane_id,
                        user_entry_id,
                        f"v2_run_group_{turn['id']}",
                        assistant_entry_id,
                        1 if is_active else 0,
                        run_status,
                        variant["created_at"],
                        variant["started_at"],
                        variant["finished_at"],
                        run_error,
                        self._safe_run_message(run_error),
                    ),
                )

                turn_id = f"v2_turn_{variant['id']}"
                turn_status, turn_error = self._model_turn_status(
                    variant["status"],
                    variant["error_code"],
                )
                connection.execute(
                    """
                    INSERT INTO v2_model_turns(
                        id, run_id, turn_index, status, provider, model,
                        request_id, input_tokens, output_tokens, created_at,
                        started_at, finished_at, error_code, safe_message
                    )
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        run_id,
                        turn_status,
                        variant["provider"],
                        variant["model"],
                        variant["id"],
                        variant["input_tokens"],
                        variant["output_tokens"],
                        variant["created_at"],
                        variant["started_at"],
                        variant["finished_at"],
                        turn_error,
                        self._safe_run_message(turn_error),
                    ),
                )

                variant_last_entry = assistant_entry_id
                tool_rows = connection.execute(
                    """
                    SELECT * FROM tool_calls
                    WHERE response_variant_id = ?
                    ORDER BY created_at, id
                    """,
                    (variant["id"],),
                ).fetchall()
                for tool in tool_rows:
                    tool_call_entry_id = f"v2_entry_tool_call_{tool['turn_id']}_{tool['id']}"
                    seq += 1
                    self._insert_entry(
                        connection,
                        entry_id=tool_call_entry_id,
                        conversation_id=conversation_id,
                        parent_id=variant_last_entry,
                        lane_id=lane_id,
                        seq=seq,
                        entry_type="tool_call",
                        actor="assistant",
                        status="final",
                        created_at=tool["created_at"],
                        payload={
                            "toolName": tool["tool_name"],
                            "arguments": json.loads(tool["arguments_json"]),
                            "approvalMode": tool["approval_mode"],
                        },
                        source_run_id=run_id,
                    )

                    tool_result_entry_id = (
                        f"v2_entry_tool_result_{tool['turn_id']}_{tool['id']}"
                    )
                    seq += 1
                    self._insert_entry(
                        connection,
                        entry_id=tool_result_entry_id,
                        conversation_id=conversation_id,
                        parent_id=tool_call_entry_id,
                        lane_id=lane_id,
                        seq=seq,
                        entry_type="tool_result",
                        actor="tool",
                        status="final",
                        created_at=tool["finished_at"] or tool["created_at"],
                        payload={
                            "toolCallEntryId": tool_call_entry_id,
                            "status": tool["status"],
                            "output": "",
                            "error": tool["error_code"],
                            "truncated": bool(tool["result_truncated"]),
                        },
                        source_run_id=run_id,
                    )
                    execution_id = f"v2_tool_{tool['turn_id']}_{tool['id']}"
                    connection.execute(
                        """
                        INSERT INTO v2_tool_executions(
                            id, model_turn_id, call_id, tool_name,
                            arguments_hash, arguments_json, status,
                            created_at, started_at, finished_at,
                            error_code, result_entry_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            execution_id,
                            turn_id,
                            tool["id"],
                            tool["tool_name"],
                            self._arguments_hash(tool["arguments_json"]),
                            self._canonical_arguments(tool["arguments_json"]),
                            self._tool_status(tool["status"]),
                            tool["created_at"],
                            tool["started_at"],
                            tool["finished_at"],
                            tool["error_code"],
                            tool_result_entry_id,
                        ),
                    )
                    variant_last_entry = tool_result_entry_id

                if is_active:
                    active_variant_last_entry = variant_last_entry
                    active_run_id = run_id

            current_parent = active_variant_last_entry

        current_parent = self._migrate_summaries(
            connection,
            conversation,
            lane_id,
            target_conversation_id,
            current_parent,
            warnings,
        )
        connection.execute(
            "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
            (current_parent, lane_id),
        )
        return (
            current_parent,
            active_run_id,
            snapshot_source_entry_ids,
            snapshot_copied_entry_ids,
        )

    def _copy_context_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        target_conversation_id: str,
        lane_id: str,
        source_leaf_entry_id: str,
        warnings: list[str],
    ) -> tuple[Optional[str], tuple[str, ...], tuple[str, ...]]:
        source_entries = self._v2_entry_context_rows(
            connection,
            source_leaf_entry_id,
            warnings,
        )
        copied_by_source_id: dict[str, str] = {}
        copied_entry_ids: list[str] = []
        for seq, source_entry in enumerate(source_entries, start=1):
            copied_entry_id = (
                f"v2_entry_snapshot_{target_conversation_id}_{source_entry['id']}"
            )
            copied_by_source_id[source_entry["id"]] = copied_entry_id
            copied_entry_ids.append(copied_entry_id)
            source_parent_id = source_entry["parent_id"]
            parent_id = copied_by_source_id.get(source_parent_id) if source_parent_id else None
            display = self._json_object(source_entry["display_json"])
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
                    target_conversation_id,
                    parent_id,
                    lane_id,
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
        return (
            copied_entry_ids[-1] if copied_entry_ids else None,
            tuple(row["id"] for row in source_entries),
            tuple(copied_entry_ids),
        )

    def _v2_entry_context_rows(
        self,
        connection: sqlite3.Connection,
        leaf_entry_id: str,
        warnings: list[str],
    ) -> tuple[sqlite3.Row, ...]:
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        current_id: Optional[str] = leaf_entry_id
        while current_id is not None:
            if current_id in seen:
                warnings.append(
                    f"Transcript context for {leaf_entry_id} contains a cycle"
                )
                break
            seen.add(current_id)
            row = connection.execute(
                "SELECT * FROM v2_transcript_entries WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                warnings.append(
                    f"Transcript context for {leaf_entry_id} references missing entry {current_id}"
                )
                break
            rows.append(row)
            current_id = row["parent_id"]
        rows.reverse()
        return tuple(rows)

    def _insert_temporary_conversation_record(
        self,
        connection: sqlite3.Connection,
        *,
        conversation: sqlite3.Row,
        source_conversation_id: Optional[str],
        source_lane_id: Optional[str],
        source_leaf_entry_id: Optional[str],
        snapshot_source_entry_ids: Sequence[str],
        snapshot_copied_entry_ids: Sequence[str],
    ) -> None:
        source_base_entry_id: Optional[str] = None
        if source_lane_id is not None:
            source_lane = connection.execute(
                "SELECT base_entry_id FROM v2_lanes WHERE id = ?",
                (source_lane_id,),
            ).fetchone()
            if source_lane is not None:
                source_base_entry_id = source_lane["base_entry_id"]

        connection.execute(
            """
            INSERT INTO v2_temporary_conversations(
                conversation_id, source_conversation_id, source_lane_id,
                source_base_entry_id, source_leaf_entry_id,
                snapshot_entry_ids_json, created_at, promoted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                conversation["id"],
                source_conversation_id,
                source_lane_id,
                source_base_entry_id,
                source_leaf_entry_id,
                _dump(
                    {
                        "entryIds": list(snapshot_source_entry_ids),
                        "copiedEntryIds": list(snapshot_copied_entry_ids),
                    }
                ),
                conversation["created_at"],
            ),
        )

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}

    def _migrate_summaries(
        self,
        connection: sqlite3.Connection,
        conversation: sqlite3.Row,
        lane_id: str,
        target_conversation_id: str,
        current_parent: Optional[str],
        warnings: list[str],
    ) -> Optional[str]:
        summary_rows = connection.execute(
            """
            SELECT * FROM conversation_summary_revisions
            WHERE conversation_id = ?
            ORDER BY created_at, id
            """,
            (conversation["id"],),
        ).fetchall()
        for summary in summary_rows:
            summary_entry_id = f"v2_entry_summary_{summary['id']}"
            previous_parent = current_parent
            connection.execute(
                """
                INSERT INTO v2_transcript_entries(
                    id, conversation_id, parent_id, lane_id, seq, type,
                    type_version, actor, status, created_at, updated_at,
                    payload_json, context_policy_json, display_json, source_run_id
                )
                VALUES (?, ?, ?, ?, ?, 'context_summary', 1, 'runtime', 'final',
                        ?, ?, ?, ?, '{}', NULL)
                """,
                (
                    summary_entry_id,
                    target_conversation_id,
                    current_parent,
                    lane_id,
                    self._next_entry_seq(connection, lane_id),
                    summary["created_at"],
                    summary["created_at"],
                    _dump(
                        {
                            "content": summary["content"],
                            "promptVersion": summary["prompt_version"],
                            "throughTurnOrdinal": summary["through_turn_ordinal"],
                        }
                    ),
                    _dump(
                        {
                            "include_in_llm": True,
                            "transform": "summary",
                            "trust_level": "runtime_internal",
                        }
                    ),
                ),
            )
            covered_entry_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM v2_transcript_entries
                    WHERE lane_id = ? AND id != ?
                    ORDER BY seq
                    """,
                    (lane_id, summary_entry_id),
                ).fetchall()
            ]
            connection.execute(
                """
                INSERT INTO v2_context_compactions(
                    id, conversation_id, lane_id, base_entry_id,
                    summary_entry_id, covered_entry_ids_json,
                    tokens_before, tokens_after, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    f"v2_compaction_{summary['id']}",
                    target_conversation_id,
                    lane_id,
                    previous_parent,
                    summary_entry_id,
                    _dump({"entryIds": covered_entry_ids}),
                    summary["input_token_estimate"],
                    summary["created_at"],
                ),
            )
            warnings.append(
                f"Summary {summary['id']} has no stored tokens_after; using 0"
            )
            current_parent = summary_entry_id
        return current_parent

    def _fork_base_entry_id(
        self,
        connection: sqlite3.Connection,
        conversation: sqlite3.Row,
        warnings: list[str],
    ) -> Optional[str]:
        parent_id = conversation["parent_conversation_id"]
        fork_turn_id = conversation["fork_turn_id"]
        if fork_turn_id is None:
            warnings.append(
                f"Branch conversation {conversation['id']} has no fork turn"
            )
            return None

        turn = connection.execute(
            "SELECT * FROM turns WHERE id = ?",
            (fork_turn_id,),
        ).fetchone()
        if turn is None or turn["conversation_id"] != parent_id:
            warnings.append(
                f"Branch conversation {conversation['id']} has an invalid fork turn"
            )
            return None

        active_variant_id = turn["active_response_variant_id"]
        if active_variant_id is not None:
            variant = connection.execute(
                "SELECT * FROM response_variants WHERE id = ?",
                (active_variant_id,),
            ).fetchone()
            if variant is not None:
                assistant = connection.execute(
                    "SELECT * FROM messages WHERE id = ?",
                    (variant["assistant_message_id"],),
                ).fetchone()
                if assistant is not None:
                    return f"v2_entry_{assistant['id']}"

        user = connection.execute(
            "SELECT * FROM messages WHERE id = ?",
            (turn["user_message_id"],),
        ).fetchone()
        if user is not None:
            return f"v2_entry_{user['id']}"
        warnings.append(
            f"Fork turn {fork_turn_id} has no migratable base entry"
        )
        return None

    def _insert_migration_lane_event(
        self,
        connection: sqlite3.Connection,
        *,
        tree_conversation_id: str,
        lane_id: str,
        conversation: sqlite3.Row,
        metadata: Mapping[str, Any],
        event_type: str = "branch.created",
    ) -> None:
        event_id = f"v2_lane_event_{conversation['id']}"
        if event_type == "branch.promoted":
            event_id = f"v2_lane_event_promoted_{conversation['id']}"
        event_seq = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1
            FROM v2_lane_events WHERE conversation_id = ?
            """,
            (tree_conversation_id,),
        ).fetchone()[0]
        data = dict(metadata)
        data.update(
            {
                "sourceConversationId": conversation["id"],
                "treeConversationId": tree_conversation_id,
                "migratedFrom": "v1_conversation",
            }
        )
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
                tree_conversation_id,
                lane_id,
                event_seq,
                event_type,
                conversation["promoted_at"] or conversation["created_at"],
                _dump(data),
            ),
        )

    def _migrate_runtime_events(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT e.*, rv.id AS response_variant_id
            FROM runtime_events AS e
            LEFT JOIN response_variants AS rv ON rv.id = e.response_variant_id
            ORDER BY e.turn_id, e.sequence
            """
        ).fetchall()
        for row in rows:
            if row["response_variant_id"] is None:
                continue
            run_id = f"v2_run_{row['response_variant_id']}"
            payload = json.loads(row["payload_json"])
            data = dict(payload.get("data", {}))
            data["sourceEventId"] = payload.get("eventId")
            if payload.get("responseVariantId") is not None:
                data["responseVariantId"] = payload["responseVariantId"]
            if payload.get("messageId") is not None:
                data["messageId"] = payload["messageId"]
            connection.execute(
                """
                INSERT INTO v2_runtime_events(
                    event_id, run_id, model_turn_id, event_seq, event_type,
                    occurred_at, correlation_id, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    f"v2_event_{row['event_id']}",
                    run_id,
                    f"v2_turn_{row['response_variant_id']}",
                    row["sequence"],
                    row["event_type"],
                    row["occurred_at"],
                    _dump(data),
                ),
            )

    @staticmethod
    def _insert_entry(
        connection: sqlite3.Connection,
        *,
        entry_id: str,
        conversation_id: str,
        parent_id: Optional[str],
        lane_id: str,
        seq: int,
        entry_type: str,
        actor: str,
        status: str,
        created_at: str,
        payload: dict[str, Any],
        source_run_id: Optional[str] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO v2_transcript_entries(
                id, conversation_id, parent_id, lane_id, seq, type,
                type_version, actor, status, created_at, updated_at,
                payload_json, context_policy_json, display_json, source_run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                entry_id,
                conversation_id,
                parent_id,
                lane_id,
                seq,
                entry_type,
                actor,
                status,
                created_at,
                created_at,
                _dump(payload),
                _dump(
                    RuntimeV2MigrationService._context_policy_for(
                        entry_type,
                        actor,
                    )
                ),
                source_run_id,
            ),
        )

    @staticmethod
    def _context_policy_for(entry_type: str, actor: str) -> dict[str, Any]:
        if entry_type == "user_message":
            return {
                "include_in_llm": True,
                "transform": "full",
                "trust_level": "user_input",
            }
        if entry_type == "assistant_message":
            return {
                "include_in_llm": True,
                "transform": "full",
                "trust_level": "model_output",
            }
        if entry_type == "tool_result":
            return {
                "include_in_llm": True,
                "transform": "full",
                "trust_level": "tool_output",
            }
        if entry_type == "context_summary":
            return {
                "include_in_llm": True,
                "transform": "summary",
                "trust_level": "runtime_internal",
            }
        return {
            "include_in_llm": False,
            "transform": "reference",
            "trust_level": "runtime_internal" if actor == "runtime" else "model_output",
        }

    @staticmethod
    def _run_status(status: str, error_code: Optional[str]) -> tuple[str, Optional[str]]:
        if status in ("completed", "failed", "cancelled"):
            return status, error_code
        return "failed", "v1_interrupted"

    @staticmethod
    def _model_turn_status(
        status: str,
        error_code: Optional[str],
    ) -> tuple[str, Optional[str]]:
        if status == "completed":
            return "completed", None
        if status == "failed":
            return "failed", error_code
        if status == "cancelled":
            return "cancelled", error_code
        return "failed", "v1_interrupted"

    @staticmethod
    def _tool_status(status: str) -> str:
        return status

    @staticmethod
    def _safe_run_message(error_code: Optional[str]) -> Optional[str]:
        if error_code is None:
            return None
        if error_code == "v1_interrupted":
            return "This v1 execution was interrupted before migration and was not resumed."
        return f"The v1 execution ended with error {error_code}."

    @staticmethod
    def _canonical_arguments(arguments_json: str) -> str:
        value = json.loads(arguments_json)
        return _dump(value if isinstance(value, dict) else {})

    @staticmethod
    def _arguments_hash(arguments_json: str) -> str:
        import hashlib

        return hashlib.sha256(
            RuntimeV2MigrationService._canonical_arguments(arguments_json).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _next_entry_seq(connection: sqlite3.Connection, lane_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
            (lane_id,),
        ).fetchone()
        return int(row[0])

    def _load_state(self) -> Optional[RuntimeV2MigrationReport]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM v2_migration_state WHERE migration_name = ?",
                (MIGRATION_NAME,),
            ).fetchone()
        if row is None:
            return None
        return self._report_from_json(row["report_json"], already=True)

    @staticmethod
    def _count(connection: sqlite3.Connection, table: str) -> int:
        return int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    @staticmethod
    def _count_by_prefix(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        prefix: str,
    ) -> int:
        return int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ? || '%'",
                (prefix,),
            ).fetchone()[0]
        )

    @staticmethod
    def _pending_migration_count(connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM conversations AS conversation
                WHERE EXISTS (
                    SELECT 1 FROM turns AS turn
                    WHERE turn.conversation_id = conversation.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM v2_migration_conversation_mappings AS mapping
                    WHERE mapping.source_conversation_id = conversation.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM v2_conversation_pointers AS pointer
                    WHERE pointer.conversation_id = conversation.id
                )
                """
            ).fetchone()[0]
        )

    @staticmethod
    def _compare_count(
        errors: list[str],
        label: str,
        expected: int,
        actual: int,
    ) -> None:
        if expected != actual:
            errors.append(f"{label}_count_mismatch expected={expected} actual={actual}")

    @staticmethod
    def _build_report(
        connection: sqlite3.Connection,
        warnings: list[str],
    ) -> RuntimeV2MigrationReport:
        def count(table: str) -> int:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return RuntimeV2MigrationReport(
            conversation_count=count("conversations"),
            lane_count=count("v2_lanes"),
            entry_count=count("v2_transcript_entries"),
            run_count=count("v2_runs"),
            model_turn_count=count("v2_model_turns"),
            tool_execution_count=count("v2_tool_executions"),
            runtime_event_count=count("v2_runtime_events"),
            context_compaction_count=count("v2_context_compactions"),
            conversation_tree_count=int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT tree_conversation_id)
                    FROM v2_migration_conversation_mappings
                    """
                ).fetchone()[0]
            ),
            branch_conversation_count=int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM v2_migration_conversation_mappings
                    WHERE source_conversation_id <> tree_conversation_id
                    """
                ).fetchone()[0]
            ),
            conversation_mapping_count=count("v2_migration_conversation_mappings"),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _report_from_json(
        value: str,
        *,
        already: bool,
    ) -> RuntimeV2MigrationReport:
        data = json.loads(value)
        return RuntimeV2MigrationReport(
            conversation_count=int(data["conversation_count"]),
            lane_count=int(data["lane_count"]),
            entry_count=int(data["entry_count"]),
            run_count=int(data["run_count"]),
            model_turn_count=int(data["model_turn_count"]),
            tool_execution_count=int(data["tool_execution_count"]),
            runtime_event_count=int(data["runtime_event_count"]),
            context_compaction_count=int(data["context_compaction_count"]),
            conversation_tree_count=int(data.get("conversation_tree_count", 0)),
            branch_conversation_count=int(
                data.get("branch_conversation_count", 0)
            ),
            conversation_mapping_count=int(
                data.get("conversation_mapping_count", 0)
            ),
            legacy_temporary_lane_repair_count=int(
                data.get("legacy_temporary_lane_repair_count", 0)
            ),
            warnings=tuple(data.get("warnings", ())),
            already_migrated=already,
        )
