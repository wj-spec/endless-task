"""R5.7 检索埋点仓储：注入/搜索/角标点击事件的本地记录与聚合。

仅本地落库与聚合，不出机；用于零命中率等检索质量决策指标。
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Mapping, Optional

from endless_task.domain.models import RetrievalEvent, RetrievalEventKind

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


class SqliteRetrievalEventRepository:
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

    def record(
        self,
        kind: RetrievalEventKind,
        query: str,
        *,
        hit_counts: Optional[Mapping[str, int]] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        detail: Optional[Mapping[str, object]] = None,
    ) -> RetrievalEvent:
        counts = {key: int(value) for key, value in (hit_counts or {}).items()}
        zero_hit = kind is not RetrievalEventKind.CITATION_CLICK and not any(
            value > 0 for value in counts.values()
        )
        event_id = self._id_factory("revev")
        created_at = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO retrieval_events (
                    id, kind, query, conversation_id, turn_id,
                    hit_counts, zero_hit, detail, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    kind.value,
                    (query or "").strip()[:1000],
                    conversation_id,
                    turn_id,
                    json.dumps(counts, ensure_ascii=False),
                    1 if zero_hit else 0,
                    json.dumps(dict(detail or {}), ensure_ascii=False)
                    if detail is not None
                    else None,
                    created_at,
                ),
            )
        return RetrievalEvent(
            id=event_id,
            kind=kind,
            query=query,
            hit_counts=counts,
            zero_hit=zero_hit,
            created_at=created_at,
            conversation_id=conversation_id,
            turn_id=turn_id,
            detail=dict(detail or {}) if detail is not None else None,
        )

    def latest_injection_for_turn(self, turn_id: str) -> Optional[RetrievalEvent]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_events "
                "WHERE turn_id = ? AND kind = 'injection' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_recent(
        self,
        limit: int = 50,
        kind: Optional[RetrievalEventKind] = None,
    ) -> List[RetrievalEvent]:
        limit = max(1, min(limit, 500))
        query = "SELECT * FROM retrieval_events"
        params: tuple = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (kind.value,)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._database.connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [self._from_row(row) for row in rows]

    def summarize(self) -> Dict[str, object]:
        """聚合检索质量指标：注入/搜索零命中率与角标点击量。"""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, COUNT(*) AS total, SUM(zero_hit) AS zeros "
                "FROM retrieval_events GROUP BY kind"
            ).fetchall()
        stats: Dict[str, object] = {}
        for row in rows:
            total = row["total"] or 0
            zeros = row["zeros"] or 0
            stats[row["kind"]] = {
                "total": total,
                "zeroHit": zeros,
                "zeroHitRate": round(zeros / total, 4) if total else 0.0,
            }
        return stats

    @staticmethod
    def _from_row(row) -> RetrievalEvent:
        detail = None
        if row["detail"]:
            try:
                detail = json.loads(row["detail"])
            except json.JSONDecodeError:
                detail = None
        try:
            counts = json.loads(row["hit_counts"] or "{}")
        except json.JSONDecodeError:
            counts = {}
        return RetrievalEvent(
            id=row["id"],
            kind=RetrievalEventKind(row["kind"]),
            query=row["query"],
            hit_counts={str(key): int(value) for key, value in counts.items()},
            zero_hit=bool(row["zero_hit"]),
            created_at=row["created_at"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            detail=detail,
        )
