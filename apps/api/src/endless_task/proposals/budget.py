"""R5.7 全局提案预算：记忆/知识提案共享日上限、单会话冷却与静默时段。

超限静默跳过（不落提案、不打扰用户）；状态全部由既有提案表推导，
不新增状态存储。时刻比较使用本地时间（本地优先产品，静默时段按用户当地时间）。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional

from endless_task.storage.database import Database

logger = logging.getLogger(__name__)

#: 参与共享预算的提案表（记忆 + 知识；成果/任务提案有自己的确认节奏，不占预算）。
_BUDGETED_TABLES = ("memory_proposals", "knowledge_proposals")


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ProposalBudget:
    def __init__(
        self,
        database: Database,
        *,
        daily_limit: int = 6,
        cooldown_minutes: int = 30,
        quiet_start: Optional[str] = None,
        quiet_end: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if daily_limit < 0:
            raise ValueError("daily_limit cannot be negative")
        if cooldown_minutes < 0:
            raise ValueError("cooldown_minutes cannot be negative")
        self._database = database
        self._daily_limit = daily_limit
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._quiet_start = self._parse_time(quiet_start)
        self._quiet_end = self._parse_time(quiet_end)
        self._clock = clock

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[time]:
        text = (value or "").strip()
        if not text:
            return None
        try:
            hours, minutes = text.split(":", 1)
            return time(int(hours), int(minutes))
        except (ValueError, TypeError) as error:
            raise ValueError(f"Invalid quiet-hours time: {value!r}") from error

    def allow(self, conversation_id: str) -> bool:
        """是否允许当前会话此刻再产出一条记忆/知识提案。"""
        now = self._clock()
        if self._in_quiet_hours(now):
            logger.debug("Proposal budget: quiet hours, skipping.")
            return False
        if self._daily_limit == 0:
            return False
        local_now = now.astimezone()
        day_start_utc = (
            datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with self._database.connect() as connection:
            today_total = 0
            latest_for_conversation: Optional[str] = None
            for table in _BUDGETED_TABLES:
                row = connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table} "
                    "WHERE created_at >= ?",
                    (day_start_utc,),
                ).fetchone()
                today_total += row["total"] or 0
                row = connection.execute(
                    f"SELECT MAX(created_at) AS latest FROM {table} "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                latest = row["latest"] if row else None
                if latest is not None and (
                    latest_for_conversation is None
                    or latest > latest_for_conversation
                ):
                    latest_for_conversation = latest
        if today_total >= self._daily_limit:
            logger.debug(
                "Proposal budget: daily limit reached (%s/%s).",
                today_total,
                self._daily_limit,
            )
            return False
        if latest_for_conversation is not None and self._cooldown > timedelta():
            latest_dt = _parse_timestamp(latest_for_conversation)
            if latest_dt is not None and now - latest_dt < self._cooldown:
                logger.debug("Proposal budget: conversation cooling down.")
                return False
        return True

    def _in_quiet_hours(self, now: datetime) -> bool:
        if self._quiet_start is None or self._quiet_end is None:
            return False
        current = now.astimezone().time()
        if self._quiet_start <= self._quiet_end:
            return self._quiet_start <= current < self._quiet_end
        return current >= self._quiet_start or current < self._quiet_end
