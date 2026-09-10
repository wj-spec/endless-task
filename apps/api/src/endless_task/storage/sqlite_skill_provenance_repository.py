"""S8：生态安装来源记录（provenance）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .database import Database
from .sqlite_chat_repository import utc_now

Clock = Callable[[], str]


@dataclass(frozen=True)
class SkillProvenance:
    scope: str
    workspace_id: str
    name: str
    spec: str
    source: str
    ref: str
    digest: str
    worst_level: Optional[str]
    installed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "name": self.name,
            "spec": self.spec,
            "source": self.source,
            "ref": self.ref,
            "digest": self.digest,
            "worstLevel": self.worst_level,
            "installedAt": self.installed_at,
        }


class SqliteSkillProvenanceRepository:
    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._clock = clock

    def record(
        self,
        *,
        scope: str,
        workspace_id: str,
        name: str,
        spec: str,
        source: str,
        digest: str,
        ref: str = "HEAD",
        worst_level: Optional[str] = None,
    ) -> SkillProvenance:
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO skill_provenance(
                    scope, workspace_id, name, spec, source, ref,
                    digest, worst_level, installed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, workspace_id, name) DO UPDATE SET
                    spec = excluded.spec,
                    source = excluded.source,
                    ref = excluded.ref,
                    digest = excluded.digest,
                    worst_level = excluded.worst_level,
                    installed_at = excluded.installed_at
                """,
                (
                    scope,
                    workspace_id,
                    name,
                    spec,
                    source,
                    ref,
                    digest,
                    worst_level,
                    now,
                ),
            )
        return SkillProvenance(
            scope=scope,
            workspace_id=workspace_id,
            name=name,
            spec=spec,
            source=source,
            ref=ref,
            digest=digest,
            worst_level=worst_level,
            installed_at=now,
        )

    def get(
        self, *, scope: str, workspace_id: str, name: str
    ) -> Optional[SkillProvenance]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM skill_provenance "
                "WHERE scope = ? AND workspace_id = ? AND name = ?",
                (scope, workspace_id, name),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_all(self) -> Tuple[SkillProvenance, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_provenance ORDER BY installed_at DESC"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)


def _from_row(row) -> SkillProvenance:
    return SkillProvenance(
        scope=row["scope"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        spec=row["spec"],
        source=row["source"],
        ref=row["ref"],
        digest=row["digest"],
        worst_level=row["worst_level"],
        installed_at=row["installed_at"],
    )
