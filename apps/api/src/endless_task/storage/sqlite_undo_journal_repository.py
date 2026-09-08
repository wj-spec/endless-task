"""A5 撤销日志：可逆工具副作用的"变更前状态"。

只记录**可逆**的文件写/删；外部动作、命令副作用不入表（不可撤销）。
写入是 best-effort：撤销日志失败不能影响工具结果本身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from endless_task.domain.repositories import NotFoundError, ValidationError

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

FILE_WRITE = "file_write"
FILE_DELETE = "file_delete"
AVAILABLE = "available"
UNDONE = "undone"

_KINDS = (FILE_WRITE, FILE_DELETE)


@dataclass(frozen=True)
class UndoJournalEntry:
    id: str
    conversation_id: str
    kind: str
    target: str
    workspace_root: str
    before_exists: bool
    description: str
    status: str
    created_at: str
    workspace_id: Optional[str] = None
    run_id: Optional[str] = None
    tool_execution_id: Optional[str] = None
    before_content: Optional[str] = None
    after_hash: Optional[str] = None
    undone_at: Optional[str] = None

    @property
    def undoable(self) -> bool:
        return self.status == AVAILABLE


def _from_row(row) -> UndoJournalEntry:
    return UndoJournalEntry(
        id=row["id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        target=row["target"],
        workspace_root=row["workspace_root"],
        before_exists=bool(row["before_exists"]),
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        tool_execution_id=row["tool_execution_id"],
        before_content=row["before_content"],
        after_hash=row["after_hash"],
        undone_at=row["undone_at"],
    )


class SqliteUndoJournalRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def append(
        self,
        *,
        conversation_id: str,
        kind: str,
        target: str,
        workspace_root: str,
        before_exists: bool,
        description: str,
        workspace_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_execution_id: Optional[str] = None,
        before_content: Optional[str] = None,
        after_hash: Optional[str] = None,
    ) -> UndoJournalEntry:
        if kind not in _KINDS:
            raise ValidationError(f"Unsupported undo kind: {kind!r}")
        if not conversation_id.strip() or not target.strip():
            raise ValidationError("Undo entries need a conversation and a target.")
        entry_id = self._id_factory("undo")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO undo_journal (
                    id, conversation_id, workspace_id, run_id,
                    tool_execution_id, kind, target, workspace_root,
                    before_exists, before_content, after_hash, description,
                    status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    conversation_id,
                    workspace_id,
                    run_id,
                    tool_execution_id,
                    kind,
                    target,
                    workspace_root,
                    1 if before_exists else 0,
                    before_content,
                    after_hash,
                    description,
                    AVAILABLE,
                    now,
                ),
            )
        return self.get(entry_id)

    def get(self, entry_id: str) -> UndoJournalEntry:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM undo_journal WHERE id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Undo entry not found: {entry_id}")
        return _from_row(row)

    def list_for_conversation(
        self, conversation_id: str, *, limit: int = 10
    ) -> Sequence[UndoJournalEntry]:
        if limit <= 0:
            return ()
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM undo_journal
                WHERE conversation_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, int(limit)),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def latest_available(
        self, conversation_id: str
    ) -> Optional[UndoJournalEntry]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM undo_journal
                WHERE conversation_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id, AVAILABLE),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def mark_undone(self, entry_id: str) -> UndoJournalEntry:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM undo_journal WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Undo entry not found: {entry_id}")
            if row["status"] != AVAILABLE:
                return _from_row(row)
            connection.execute(
                """
                UPDATE undo_journal
                SET status = ?, undone_at = ?
                WHERE id = ?
                """,
                (UNDONE, now, entry_id),
            )
        return self.get(entry_id)


__all__ = [
    "AVAILABLE",
    "FILE_DELETE",
    "FILE_WRITE",
    "UNDONE",
    "SqliteUndoJournalRepository",
    "UndoJournalEntry",
]
