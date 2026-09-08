"""B5 用户画像仓储：一份画像一行，按 scope_key 唯一。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

#: 未绑定工作区时的默认画像键。
GENERAL_SCOPE_KEY = "general"


@dataclass(frozen=True)
class UserProfileRecord:
    scope_key: str
    content: str
    lines: tuple[str, ...]
    signature: str
    version: int
    manual: bool
    created_at: str
    updated_at: str
    source_memory_ids: tuple[str, ...] = ()


def _from_row(row) -> UserProfileRecord:
    try:
        lines = tuple(json.loads(row["lines_json"]))
    except (TypeError, ValueError):
        lines = ()
    try:
        sources = tuple(json.loads(row["source_memory_ids"]))
    except (TypeError, ValueError):
        sources = ()
    return UserProfileRecord(
        scope_key=row["scope_key"],
        content=row["content"],
        lines=tuple(str(item) for item in lines),
        signature=row["signature"],
        version=int(row["version"]),
        manual=bool(row["manual"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_memory_ids=tuple(str(item) for item in sources),
    )


class SqliteUserProfileRepository:
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

    def get(self, scope_key: str) -> Optional[UserProfileRecord]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def upsert(
        self,
        *,
        scope_key: str,
        content: str,
        lines: Sequence[str],
        signature: str,
        version: int,
        manual: bool = False,
        source_memory_ids: Sequence[str] = (),
    ) -> UserProfileRecord:
        now = self._clock()
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM user_profiles WHERE scope_key = ?", (scope_key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO user_profiles (
                        scope_key, content, lines_json, signature, version,
                        manual, source_memory_ids, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope_key,
                        content,
                        json.dumps(list(lines), ensure_ascii=False),
                        signature,
                        max(1, int(version)),
                        1 if manual else 0,
                        json.dumps(list(source_memory_ids), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE user_profiles
                    SET content = ?, lines_json = ?, signature = ?, version = ?,
                        manual = ?, source_memory_ids = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (
                        content,
                        json.dumps(list(lines), ensure_ascii=False),
                        signature,
                        max(1, int(version)),
                        1 if manual else 0,
                        json.dumps(list(source_memory_ids), ensure_ascii=False),
                        now,
                        scope_key,
                    ),
                )
        record = self.get(scope_key)
        assert record is not None
        return record

    def list_records(self) -> Sequence[UserProfileRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM user_profiles ORDER BY scope_key"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def delete(self, scope_key: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "DELETE FROM user_profiles WHERE scope_key = ?", (scope_key,)
            )


__all__ = [
    "GENERAL_SCOPE_KEY",
    "SqliteUserProfileRepository",
    "UserProfileRecord",
]
