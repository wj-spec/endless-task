"""P1-2 hub 全局事件仓储。

hub 角标（未读通知 + pending proposals）是 App 顶层全局消费，跨会话聚合；
与 conversation-scoped 的 v2_product_events 不同，hub 事件挂在全库唯一
event_seq 游标上，SSE 端点按 after_seq 增量订阅。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


@dataclass(frozen=True)
class HubEventRecord:
    id: str
    event_seq: int
    event_type: str
    conversation_id: Optional[str]
    data: dict[str, Any]
    occurred_at: str


def _loads(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def hub_event_from_row(row) -> HubEventRecord:
    return HubEventRecord(
        id=row["id"],
        event_seq=int(row["event_seq"]),
        event_type=row["event_type"],
        conversation_id=row["conversation_id"],
        data=_loads(row["data"]),
        occurred_at=row["occurred_at"],
    )


class SqliteHubEventRepository:
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
        event_type: str,
        *,
        conversation_id: Optional[str] = None,
        data: Optional[Mapping[str, object]] = None,
    ) -> HubEventRecord:
        event_id = self._id_factory("hub")
        occurred_at = self._clock()
        serialized = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))

        def operation(connection) -> HubEventRecord:
            event_seq = self._next_seq(connection)
            connection.execute(
                """
                INSERT INTO hub_events(
                    id, event_seq, event_type, conversation_id, data, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_seq,
                    event_type,
                    conversation_id,
                    serialized,
                    occurred_at,
                ),
            )
            return hub_event_from_row(
                connection.execute(
                    "SELECT * FROM hub_events WHERE id = ?", (event_id,)
                ).fetchone()
            )

        with self._database.transaction() as connection:
            return operation(connection)

    def list_after(
        self,
        after_seq: int = 0,
        *,
        limit: int = 200,
    ) -> tuple[HubEventRecord, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM hub_events
                WHERE event_seq > ?
                ORDER BY event_seq ASC
                LIMIT ?
                """,
                (after_seq, limit),
            ).fetchall()
        return tuple(hub_event_from_row(row) for row in rows)

    def latest_seq(self) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_seq), 0) AS seq FROM hub_events"
            ).fetchone()
        return int(row["seq"])

    def _next_seq(self, connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) AS seq FROM hub_events"
        ).fetchone()
        return int(row["seq"]) + 1
