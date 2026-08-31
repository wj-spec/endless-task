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

    def bind_root_path(self, workspace_id: str, root_path: str) -> Workspace:
        from pathlib import Path

        from endless_task.workspace_runtime.path_safety import validate_bind_root

        normalized = (root_path or "").strip()
        if not normalized:
            raise ValidationError("Workspace root path is required.")
        reason = validate_bind_root(Path(normalized))
        if reason is not None:
            raise ValidationError(reason)
        resolved = str(Path(normalized).expanduser().resolve(strict=True))
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Workspace not found: {workspace_id}")
            other = connection.execute(
                "SELECT id FROM workspaces WHERE root_path = ? AND id != ?",
                (resolved, workspace_id),
            ).fetchone()
            if other is not None:
                raise ConflictError(
                    "This directory is already bound to another workspace."
                )
            now = self._clock()
            connection.execute(
                "UPDATE workspaces SET root_path = ?, updated_at = ? WHERE id = ?",
                (resolved, now, workspace_id),
            )
        return self.get_workspace(workspace_id)

    def unbind_root_path(self, workspace_id: str) -> Workspace:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Workspace not found: {workspace_id}")
            now = self._clock()
            connection.execute(
                "UPDATE workspaces SET root_path = NULL, updated_at = ? WHERE id = ?",
                (now, workspace_id),
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

    def delete_workspace(self, workspace_id: str) -> bool:
        """删除工作区注册记录；仅删除注册，绝不触碰目录/会话日志。

        返回 False 表示 id 不存在。调用方需先确认该工作区没有承载会话
        （否则应先迁移其会话）。
        """
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
            )
        return True

    @staticmethod
    def _from_row(row) -> Workspace:
        return Workspace(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            root_path=row["root_path"],
        )
