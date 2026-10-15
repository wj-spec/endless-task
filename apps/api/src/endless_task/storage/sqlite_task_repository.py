"""SQLite persistence for P4 tasks (R4.0: create/read/list only)."""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, Optional, Sequence, Union

from endless_task.domain.models import TaskRecord, TaskStatus
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from endless_task.domain.task_schedule import (
    TaskSchedule,
    parse_task_schedule,
    serialize_task_schedule,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def insert_task_row(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    title: str,
    commitment: str,
    schedule_json: str,
    source_conversation_id: str,
    source_turn_id: str,
    source_proposal_id: Optional[str],
    now: str,
) -> None:
    """`tasks` 表的**唯一**写入器（切片 10 T5）。

    谁建任务都必须落到这里，否则就会在库里出现第三种行形状：

    - `SqliteTaskRepository.create_task`（**显式创建的缝**：HTTP `POST /tasks` 走它）；
    - `SqliteTaskProposalRepository.accept_proposal`（**提案接受**的缝，此前自己
      内联了一份同样的 INSERT）。

    本函数只负责"一行 `tasks` 怎么写"（列集合、列序、默认值），**不做**校验与
    序列化：`create_task` 在调用前完成 title/commitment 归一化与
    `serialize_task_schedule`；`accept_proposal` 复用提案在 `create_proposal`
    时已归一化的值、以及库中已存的 schedule JSON 字节（保持逐字节不变）。
    校验一律发生在**任何写入之前**，因此非法输入不会留下半行。

    调用方必须已经处于自己的事务里：`create_task` 用它包住单次 INSERT，
    `accept_proposal` 用它把"建任务 + 标记提案 accepted"保持在同一事务中
    （原子性——不能拆成两个事务，否则崩溃窗口会留下"任务已建、提案仍 pending"，
    用户再点一次接受就会建出第二个任务）。
    """
    connection.execute(
        """
        INSERT INTO tasks (
            id, title, commitment, schedule, status,
            source_conversation_id, source_turn_id, source_proposal_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            title,
            commitment,
            schedule_json,
            TaskStatus.ACTIVE.value,
            source_conversation_id,
            source_turn_id,
            source_proposal_id,
            now,
            now,
        ),
    )


def task_record_from_row(row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        title=row["title"],
        commitment=row["commitment"],
        schedule=parse_task_schedule(json.loads(row["schedule"])),
        status=TaskStatus(row["status"]),
        source_conversation_id=row["source_conversation_id"],
        source_turn_id=row["source_turn_id"],
        source_proposal_id=row["source_proposal_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancelled_at=row["cancelled_at"],
        resumed_at=row["resumed_at"],
    )


class SqliteTaskRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 200,
        max_commitment_chars: int = 2000,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_title_chars <= 0 or max_commitment_chars <= 0:
            raise ValueError("Task limits must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_commitment_chars = max_commitment_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_task(
        self,
        *,
        title: str,
        commitment: str,
        schedule: Union[str, dict, TaskSchedule],
        source_conversation_id: str,
        source_turn_id: str,
        source_proposal_id: Optional[str] = None,
    ) -> TaskRecord:
        normalized_title = self._validate_text(
            title, self._max_title_chars, field="title"
        )
        normalized_commitment = self._validate_text(
            commitment, self._max_commitment_chars, field="commitment"
        )
        parsed_schedule = parse_task_schedule(schedule)
        if not source_conversation_id.strip() or not source_turn_id.strip():
            raise ValidationError("Task source conversation and turn are required.")
        if source_proposal_id is not None and not source_proposal_id.strip():
            raise ValidationError("Task source proposal id must not be blank.")
        schedule_json = json.dumps(
            serialize_task_schedule(parsed_schedule),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = self._clock()
        task_id = self._id_factory("task")
        with self._database.transaction() as connection:
            insert_task_row(
                connection,
                task_id=task_id,
                title=normalized_title,
                commitment=normalized_commitment,
                schedule_json=schedule_json,
                source_conversation_id=source_conversation_id.strip(),
                source_turn_id=source_turn_id.strip(),
                source_proposal_id=(
                    source_proposal_id.strip() if source_proposal_id else None
                ),
                now=now,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return task_record_from_row(row)

    def list_tasks(self, *, include_cancelled: bool = False) -> Sequence[TaskRecord]:
        query = "SELECT * FROM tasks"
        if not include_cancelled:
            query += " WHERE status IN ('active', 'paused')"
        query += " ORDER BY updated_at DESC, id ASC"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(task_record_from_row(row) for row in rows)

    def cancel_tasks_for_conversation(self, conversation_id: str) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, cancelled_at = ?, updated_at = ?
                WHERE source_conversation_id = ?
                    AND status IN ('active', 'paused')
                """,
                (
                    TaskStatus.CANCELLED.value,
                    now,
                    now,
                    conversation_id.strip(),
                ),
            )
        return cursor.rowcount or 0

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        commitment: Optional[str] = None,
        schedule: Union[str, dict, TaskSchedule, None] = None,
    ) -> TaskRecord:
        """编辑一条定时任务的**可编辑语义字段**（批次 D Task 1，R-D6 / R-D7）。

        只接受 `title` / `commitment` / `schedule`，**一个都不传**是调用方的
        错误（HTTP 层先翻成 `ValidationError("Nothing to update.")`）。`status`
        **刻意不在这里**——它与 `pause_task` / `resume_task` / `cancel_task` 的
        `_transition` 会形成两个入口、两套允许集。`source_conversation_id` /
        `source_turn_id` / `source_proposal_id` / `created_at` 也**不动**：前两者
        是执行环境来源（`tasks/worker.py:_ensure_branch` 用它继承 workspace /
        provider / model），`source_proposal_id` 是审计锚点，`created_at` 是触发
        anchor 的一部分。

        **`resumed_at` 的语义在这里被扩张**：它原本只表示"从暂停恢复过"
        （`resume_task` 写它，`TaskScheduler.is_due` 把它并进 anchor）。本方法起
        它还表示"**排期变过**"——只要 `schedule` 真的变了，就在同一个事务里把它
        写成 `now`，把触发 anchor 重置到"编辑这一刻"。

        为什么必须重置（R-D7）：`scan_once` 是**无状态推导**，库里没有
        `next_run_at`，anchor = max(`created_at`, `resumed_at`, 最近 scheduled
        run.`started_at`)。若改期不动 anchor，把时间改到"相对旧 anchor 已过去"
        的时刻会**立刻补跑一次**（用户改期 → 立刻执行，反直觉）。

        判"变了"用的是**解析后的语义**（`TaskSchedule` 是 frozen dataclass，
        相等即同一安排），不是"请求里带没带这个字段"：编辑表单把当前排期原样
        读回再写回是正常用法，那种情况下只改 title/commitment 不该挪 anchor。

        校验（title/commitment 非空与长度、`schedule` 的频度/时间/越界）都在
        **任何写入之前**完成 → 非法输入不会留下半行、也不改 `updated_at`。
        `cancelled` 是终态 → `InvalidStateError`（HTTP 409，与 `_transition` 的
        允许集对齐）。
        """
        normalized_title = (
            self._validate_text(title, self._max_title_chars, field="title")
            if title is not None
            else None
        )
        normalized_commitment = (
            self._validate_text(
                commitment, self._max_commitment_chars, field="commitment"
            )
            if commitment is not None
            else None
        )
        parsed_schedule = (
            parse_task_schedule(schedule) if schedule is not None else None
        )
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT status, schedule FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if TaskStatus(row["status"]) is TaskStatus.CANCELLED:
                raise InvalidStateError(
                    f"Task {task_id} is cancelled and cannot be updated."
                )
            assignments: list[str] = []
            parameters: list[object] = []
            if normalized_title is not None:
                assignments.append("title = ?")
                parameters.append(normalized_title)
            if normalized_commitment is not None:
                assignments.append("commitment = ?")
                parameters.append(normalized_commitment)
            schedule_changed = parsed_schedule is not None and (
                parsed_schedule
                != parse_task_schedule(json.loads(row["schedule"]))
            )
            if schedule_changed:
                assignments.append("schedule = ?")
                parameters.append(
                    json.dumps(
                        serialize_task_schedule(parsed_schedule),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                # R-D7：改期与重置 anchor **同一个事务**，不留"schedule 已改、
                # anchor 还是旧的"这种中间态。
                assignments.append("resumed_at = ?")
                parameters.append(now)
            assignments.append("updated_at = ?")
            parameters.append(now)
            parameters.append(task_id)
            connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                tuple(parameters),
            )
        return self.get_task(task_id)

    def pause_task(self, task_id: str) -> TaskRecord:
        return self._transition(task_id, TaskStatus.PAUSED)

    def resume_task(self, task_id: str) -> TaskRecord:
        return self._transition(task_id, TaskStatus.ACTIVE)

    def cancel_task(self, task_id: str) -> TaskRecord:
        return self._transition(task_id, TaskStatus.CANCELLED)

    def _transition(self, task_id: str, target: TaskStatus) -> TaskRecord:
        allowed = {
            TaskStatus.PAUSED: (TaskStatus.ACTIVE,),
            TaskStatus.ACTIVE: (TaskStatus.PAUSED,),
            TaskStatus.CANCELLED: (TaskStatus.ACTIVE, TaskStatus.PAUSED),
        }[target]
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if TaskStatus(row["status"]) not in allowed:
                raise InvalidStateError(
                    f"Task {task_id} cannot move to {target.value}."
                )
            if target is TaskStatus.CANCELLED:
                connection.execute(
                    "UPDATE tasks SET status = ?, cancelled_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (target.value, now, now, task_id),
                )
            elif target is TaskStatus.ACTIVE:
                connection.execute(
                    "UPDATE tasks SET status = ?, resumed_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (target.value, now, now, task_id),
                )
            else:
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (target.value, now, task_id),
                )
        return self.get_task(task_id)

    def _validate_text(self, value: str, limit: int, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"Task {field} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValidationError(f"Task {field} must not be empty.")
        if len(normalized) > limit:
            raise ValidationError(f"Task {field} exceeds {limit} characters.")
        return normalized
