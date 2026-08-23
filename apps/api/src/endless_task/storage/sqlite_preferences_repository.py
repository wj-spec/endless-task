from __future__ import annotations

from typing import Callable, Tuple

from endless_task.domain.models import PermissionMode

from .database import Database
from .sqlite_chat_repository import utc_now

Clock = Callable[[], str]


class SqlitePreferencesRepository:
    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._clock = clock

    def get_permission_mode(self) -> Tuple[PermissionMode, str]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT permission_mode, updated_at FROM preferences WHERE id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Preferences row is missing")
        return PermissionMode(row["permission_mode"]), row["updated_at"]

    def set_permission_mode(self, mode: PermissionMode) -> Tuple[PermissionMode, str]:
        if not isinstance(mode, PermissionMode):
            mode = PermissionMode(mode)
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE preferences SET permission_mode = ?, updated_at = ? "
                "WHERE id = 1",
                (mode.value, now),
            )
        return self.get_permission_mode()
