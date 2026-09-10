"""技能启用/禁用覆盖仓储：扫描目录即事实，仅用户开关落库。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from .database import Database
from .sqlite_chat_repository import utc_now

Clock = Callable[[], str]


@dataclass(frozen=True)
class SkillOverride:
    scope: str
    workspace_id: str
    name: str
    disabled: bool
    updated_at: str
    #: S5：是否固定进默认目录。
    pinned: bool = False


class SqliteSkillOverrideRepository:
    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._clock = clock

    def list_overrides(self) -> Tuple[SkillOverride, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT scope, workspace_id, name, disabled, pinned, updated_at "
                "FROM skill_overrides ORDER BY updated_at"
            ).fetchall()
        return tuple(
            SkillOverride(
                scope=row["scope"],
                workspace_id=row["workspace_id"],
                name=row["name"],
                disabled=bool(row["disabled"]),
                pinned=bool(row["pinned"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def pinned_index(self) -> Dict[Tuple[str, str], set]:
        """(scope, workspace_id) -> 固定进默认目录的技能名集合。"""
        index: Dict[Tuple[str, str], set] = {}
        for override in self.list_overrides():
            if not override.pinned:
                continue
            index.setdefault((override.scope, override.workspace_id), set()).add(
                override.name
            )
        return index

    def disabled_index(self) -> Dict[Tuple[str, str], set]:
        """(scope, workspace_id) -> 被禁用的技能名集合。user 级 workspace_id 为 ""。"""
        index: Dict[Tuple[str, str], set] = {}
        for override in self.list_overrides():
            if not override.disabled:
                continue
            index.setdefault(
                (override.scope, override.workspace_id), set()
            ).add(override.name)
        return index

    def set_pinned(
        self, *, scope: str, workspace_id: str, name: str, pinned: bool
    ) -> SkillOverride:
        """S5：固定/取消固定到默认目录（与 disabled 共用一行）。"""
        if scope not in ("user", "workspace"):
            raise ValueError("Skill override scope must be user or workspace")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO skill_overrides "
                "(scope, workspace_id, name, disabled, pinned, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(scope, workspace_id, name) DO UPDATE SET "
                "pinned = excluded.pinned, updated_at = excluded.updated_at",
                (scope, workspace_id, name, 1 if pinned else 0, now),
            )
        return SkillOverride(
            scope=scope,
            workspace_id=workspace_id,
            name=name,
            disabled=False,
            pinned=pinned,
            updated_at=now,
        )

    def set_disabled(
        self, *, scope: str, workspace_id: str, name: str, disabled: bool
    ) -> SkillOverride:
        if scope not in ("user", "workspace"):
            raise ValueError("Skill override scope must be user or workspace")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO skill_overrides "
                "(scope, workspace_id, name, disabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, workspace_id, name) DO UPDATE SET "
                "disabled = excluded.disabled, updated_at = excluded.updated_at",
                (scope, workspace_id, name, 1 if disabled else 0, now),
            )
        return SkillOverride(
            scope=scope,
            workspace_id=workspace_id,
            name=name,
            disabled=disabled,
            updated_at=now,
        )
