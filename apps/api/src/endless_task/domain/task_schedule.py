"""Task schedule protocol (R4.0).

Tasks are recurring commitments only. One-off timed matters are reminders,
not tasks (see docs/technical/p4-task-intent-protocol.md §3).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from .repositories import ValidationError


class TaskScheduleKind(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class TaskSchedule:
    kind: TaskScheduleKind
    time: str
    weekday: Optional[int] = None
    day: Optional[int] = None


_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ONCE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$"
)


def serialize_task_schedule(schedule: TaskSchedule) -> dict:
    payload: dict = {"kind": schedule.kind.value, "time": schedule.time}
    if schedule.weekday is not None:
        payload["weekday"] = schedule.weekday
    if schedule.day is not None:
        payload["day"] = schedule.day
    return payload


def parse_task_schedule(raw: Union[str, dict, TaskSchedule]) -> TaskSchedule:
    if isinstance(raw, TaskSchedule):
        return raw
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise ValidationError("任务周期必须是合法的 JSON。") from error
    if not isinstance(payload, dict):
        raise ValidationError("任务周期必须是一个对象。")

    kind_raw = payload.get("kind")
    if kind_raw == "once":
        raise ValidationError("一次性事项不属于任务，任务只支持周期安排。")
    try:
        kind = TaskScheduleKind(kind_raw)
    except ValueError as error:
        raise ValidationError(
            "任务周期的 kind 必须是 daily / weekly / monthly。"
        ) from error

    allowed_keys = {"kind", "time"}
    if kind is TaskScheduleKind.WEEKLY:
        allowed_keys.add("weekday")
    if kind is TaskScheduleKind.MONTHLY:
        allowed_keys.add("day")
    extra_keys = set(payload.keys()) - allowed_keys
    if extra_keys:
        raise ValidationError(
            "任务周期包含不支持的字段：" + ", ".join(sorted(extra_keys))
        )

    time_raw = payload.get("time")
    if not isinstance(time_raw, str) or not _TIME_PATTERN.match(time_raw):
        raise ValidationError("任务周期的 time 必须是 HH:MM（24 小时制）。")

    weekday: Optional[int] = None
    day: Optional[int] = None
    if kind is TaskScheduleKind.WEEKLY:
        weekday = payload.get("weekday")
        if (
            not isinstance(weekday, int)
            or isinstance(weekday, bool)
            or not 1 <= weekday <= 7
        ):
            raise ValidationError("weekly 周期的 weekday 必须是 1–7（1 = 周一）。")
    if kind is TaskScheduleKind.MONTHLY:
        day = payload.get("day")
        if (
            not isinstance(day, int)
            or isinstance(day, bool)
            or not 1 <= day <= 28
        ):
            raise ValidationError("monthly 周期的 day 必须是 1–28。")

    return TaskSchedule(kind=kind, time=time_raw, weekday=weekday, day=day)


_WEEKDAY_NAMES = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}


def describe_task_schedule(schedule: TaskSchedule) -> str:
    if schedule.kind is TaskScheduleKind.DAILY:
        return f"每天 {schedule.time}"
    if schedule.kind is TaskScheduleKind.WEEKLY:
        weekday = _WEEKDAY_NAMES.get(schedule.weekday or 1, "周一")
        return f"每{weekday} {schedule.time}"
    return f"每月 {schedule.day or 1} 日 {schedule.time}"


def next_occurrence(schedule: TaskSchedule, after: datetime) -> datetime:
    """First occurrence of the schedule strictly after ``after``.

    ``after`` must be aware; the HH:MM of the schedule is interpreted in the
    process local timezone (personal-assistant semantics).
    """
    if after.tzinfo is None:
        raise ValidationError("next_occurrence requires an aware datetime.")
    local_after = after.astimezone()
    hour, minute = (int(part) for part in schedule.time.split(":"))

    def at_time(day: datetime) -> datetime:
        return day.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    if schedule.kind is TaskScheduleKind.DAILY:
        candidate = at_time(local_after)
        return candidate if candidate > local_after else candidate + timedelta(days=1)

    if schedule.kind is TaskScheduleKind.WEEKLY:
        weekday = schedule.weekday or 1
        days_ahead = (weekday - 1 - local_after.weekday()) % 7
        candidate = at_time(local_after + timedelta(days=days_ahead))
        return candidate if candidate > local_after else candidate + timedelta(days=7)

    day = schedule.day or 1
    candidate = at_time(local_after.replace(day=day))
    if candidate > local_after:
        return candidate
    if local_after.month == 12:
        next_month = local_after.replace(
            year=local_after.year + 1, month=1, day=day
        )
    else:
        next_month = local_after.replace(month=local_after.month + 1, day=day)
    return at_time(next_month)


@dataclass(frozen=True)
class ReminderDue:
    """One-off timed matter (R4.9): local wall time YYYY-MM-DDTHH:MM."""

    at: str


def parse_reminder_due(raw: Union[str, dict, "ReminderDue"]) -> ReminderDue:
    if isinstance(raw, ReminderDue):
        return raw
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise ValidationError("提醒周期必须是合法的 JSON。") from error
    if not isinstance(payload, dict):
        raise ValidationError("提醒周期必须是一个对象。")
    if payload.get("kind") != "once":
        raise ValidationError("提醒周期的 kind 必须是 once。")
    extra_keys = set(payload.keys()) - {"kind", "at"}
    if extra_keys:
        raise ValidationError(
            "提醒周期包含不支持的字段：" + ", ".join(sorted(extra_keys))
        )
    at_raw = payload.get("at")
    if not isinstance(at_raw, str) or not _ONCE_PATTERN.match(at_raw):
        raise ValidationError("提醒周期的 at 必须是 YYYY-MM-DDTHH:MM。")
    try:
        datetime.strptime(at_raw, "%Y-%m-%dT%H:%M")
    except ValueError as error:
        raise ValidationError("提醒周期的日期不存在。") from error
    return ReminderDue(at=at_raw)


def parse_schedule_intent(
    raw: Union[str, dict, TaskSchedule, ReminderDue],
) -> Union[TaskSchedule, ReminderDue]:
    if isinstance(raw, (TaskSchedule, ReminderDue)):
        return raw
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise ValidationError("周期必须是合法的 JSON。") from error
    if isinstance(payload, dict) and payload.get("kind") == "once":
        return parse_reminder_due(payload)
    return parse_task_schedule(payload)


def serialize_schedule_intent(
    schedule: Union[TaskSchedule, ReminderDue],
) -> dict:
    if isinstance(schedule, ReminderDue):
        return {"kind": "once", "at": schedule.at}
    return serialize_task_schedule(schedule)


def describe_reminder_due(due: ReminderDue) -> str:
    moment = datetime.strptime(due.at, "%Y-%m-%dT%H:%M")
    stamp = moment.strftime("%H:%M")
    if moment.year == datetime.now().year:
        return f"{moment.month}月{moment.day}日 {stamp} 一次性"
    return f"{moment.year}年{moment.month}月{moment.day}日 {stamp} 一次性"


def reminder_due_to_utc_iso(due: ReminderDue) -> str:
    local = datetime.strptime(due.at, "%Y-%m-%dT%H:%M").astimezone()
    return (
        local.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
