"""S2 技能使用统计持久化（按 scope + name + digest 计数）。

与 `skills/usage.py` 的内存记录器语义一致，但落库以跨重启保留：
升级技能（digest 变化）后旧计数仍在，便于对比"升级前后"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from .database import Database

#: 允许的事件类型（与 migration 067 的 CHECK 一致）。
USAGE_KINDS: Tuple[str, ...] = (
    "surfaced",
    "invoked",
    "body_read",
    "missing_dependencies",
    #: S6：skill_search 命中该技能（用于校准默认目录的精选集）。
    "search_hit",
)


@dataclass(frozen=True)
class SkillUsageRow:
    scope: str
    name: str
    digest: str
    counts: Mapping[str, int] = field(default_factory=dict)
    last_at: Optional[str] = None

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "name": self.name,
            "digest": self.digest,
            "counts": dict(self.counts),
            "lastAt": self.last_at,
        }


class SqliteSkillUsageRepository:
    def __init__(self, database: Database, *, clock=None) -> None:
        self._database = database
        self._clock = clock or _utc_now

    def record(
        self,
        *,
        scope: str,
        name: str,
        digest: str,
        kind: str,
    ) -> None:
        if kind not in USAGE_KINDS:
            raise ValueError(f"Unknown skill usage kind: {kind}")
        if not scope or not name or not digest:
            return
        now = self._clock()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO skill_usage(scope, name, digest, kind, count, last_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(scope, name, digest, kind)
                DO UPDATE SET count = count + 1, last_at = excluded.last_at
                """,
                (scope, name, digest, kind, now),
            )

    def snapshot(self, *, scope: str, name: str) -> Tuple[SkillUsageRow, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT scope, name, digest, kind, count, last_at
                FROM skill_usage
                WHERE scope = ? AND name = ?
                ORDER BY digest, kind
                """,
                (scope, name),
            ).fetchall()
        return _group(rows)

    def activity_index(self, *, since: str) -> Dict[Tuple[str, str], Tuple[int, str]]:
        """S5：按 (scope, name) 汇总 `since` 之后的调用/读取次数与最近时间。

        只统计 `invoked`/`body_read`（真的用过），不统计 `surfaced`（只是进过目录）。
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT scope, name, kind, count, last_at
                FROM skill_usage
                WHERE kind IN ('invoked', 'body_read')
                """
            ).fetchall()
        index: Dict[Tuple[str, str], Tuple[int, str]] = {}
        for row in rows:
            last_at = row["last_at"]
            if not isinstance(last_at, str) or last_at < since:
                continue
            key = (str(row["scope"]), str(row["name"]))
            count, previous = index.get(key, (0, ""))
            index[key] = (
                count + int(row["count"]),
                max(previous, last_at),
            )
        return index

    def snapshot_all(self) -> Tuple[SkillUsageRow, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT scope, name, digest, kind, count, last_at
                FROM skill_usage
                ORDER BY scope, name, digest, kind
                """
            ).fetchall()
        return _group(rows)


def _group(rows) -> Tuple[SkillUsageRow, ...]:
    grouped: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for row in rows:
        key = (str(row["scope"]), str(row["name"]), str(row["digest"]))
        entry = grouped.setdefault(key, {"counts": {}, "lastAt": None})
        counts = entry["counts"]
        assert isinstance(counts, dict)
        counts[str(row["kind"])] = int(row["count"])
        last_at = row["last_at"]
        if isinstance(last_at, str) and (
            entry["lastAt"] is None or last_at > str(entry["lastAt"])
        ):
            entry["lastAt"] = last_at
    return tuple(
        SkillUsageRow(
            scope=key[0],
            name=key[1],
            digest=key[2],
            counts=entry["counts"],  # type: ignore[arg-type]
            last_at=entry["lastAt"],  # type: ignore[arg-type]
        )
        for key, entry in grouped.items()
    )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
