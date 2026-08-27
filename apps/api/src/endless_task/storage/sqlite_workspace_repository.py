"""R5.11 工作区仓储：工作区的创建与列表。

「通用」不是工作区行，是 NULL 语义；本仓储只管理具名工作区。
"""

from __future__ import annotations

from typing import Callable, Sequence

from endless_task.domain.models import Workspace
from endless_task.domain.repositories import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

MAX_WORKSPACE_NAME_CHARS = 60


class SqliteWorkspaceRepository:
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

    def create_workspace(self, name: str) -> Workspace:
        normalized = " ".join((name or "").split())
        if not normalized:
            raise ValidationError("Workspace name is required.")
        if len(normalized) > MAX_WORKSPACE_NAME_CHARS:
            raise ValidationError("Workspace name is too long.")
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM workspaces WHERE name = ?", (normalized,)
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"A workspace named {normalized!r} already exists."
                )
            workspace_id = self._id_factory("ws")
            now = self._clock()
            connection.execute(
                """
                INSERT INTO workspaces (id, name, root_path, created_at, updated_at)
                VALUES (?, ?, NULL, ?, ?)
                """,
                (workspace_id, normalized, now, now),
            )
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> Workspace:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Workspace not found: {workspace_id}")
        return self._from_row(row)

    def list_workspaces(self) -> Sequence[Workspace]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workspaces ORDER BY created_at, id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row) -> Workspace:
        return Workspace(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            root_path=row["root_path"],
        )
