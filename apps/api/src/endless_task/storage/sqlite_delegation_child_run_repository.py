"""委派子运行记录的 SQLite 实现（切片 9）。"""

from __future__ import annotations

import sqlite3
from typing import Callable, Optional, Tuple

from endless_task.delegation.child_run_store import (
    TERMINAL_CHILD_RUN_STATES,
    ChildRunRow,
    ChildRunState,
)
from endless_task.delegation.protocol import UsageSummary
from endless_task.domain.repositories import InvalidStateError, NotFoundError

from .database import Database
from .sqlite_chat_repository import utc_now

Clock = Callable[[], str]


class SqliteDelegationChildRunRepository:
    """`delegation_child_runs` 表：子运行的唯一真相。"""

    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._clock = clock

    def reserve_child_run(
        self,
        *,
        child_run_id: str,
        parent_run_id: str,
        parent_tool_call_id: str,
        parent_conversation_id: str,
        depth: int,
        capability_profile: str,
        workspace_mode: str,
        timeout_seconds: float,
        task: str,
        child_workspace_id: Optional[str] = None,
    ) -> Optional[ChildRunRow]:
        now = self._clock()
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO delegation_child_runs (
                        child_run_id, parent_run_id, parent_tool_call_id,
                        parent_conversation_id, child_workspace_id, depth,
                        capability_profile, workspace_mode, timeout_seconds,
                        task, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child_run_id,
                        parent_run_id,
                        parent_tool_call_id,
                        parent_conversation_id,
                        child_workspace_id,
                        depth,
                        capability_profile,
                        workspace_mode,
                        float(timeout_seconds),
                        task,
                        ChildRunState.RUNNING.value,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            # 只吞"同一 spawn key 已存在"这一种；其它完整性错误照旧炸。
            if (
                self.find_child_run_by_spawn_key(
                    parent_run_id=parent_run_id,
                    parent_tool_call_id=parent_tool_call_id,
                )
                is None
            ):
                raise
            return None
        return self.get_child_run(child_run_id)

    def mark_child_terminal(
        self,
        child_run_id: str,
        *,
        state: ChildRunState,
        summary: Optional[str] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        usage: Optional[UsageSummary] = None,
    ) -> ChildRunRow:
        if not isinstance(state, ChildRunState):
            raise ValueError("state 必须是 ChildRunState")
        if state is ChildRunState.RUNNING:
            raise InvalidStateError("子运行不能用 running 收尾。")
        usage = usage if usage is not None else UsageSummary()
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"子运行不存在：{child_run_id}")
            if row["state"] != ChildRunState.RUNNING.value:
                raise InvalidStateError("只有 running 的子运行可以收尾。")
            connection.execute(
                """
                UPDATE delegation_child_runs
                SET state = ?, summary = ?, error_code = ?, safe_message = ?,
                    input_tokens = ?, output_tokens = ?, model_turns = ?,
                    tool_calls = ?, terminal_at = ?, updated_at = ?
                WHERE child_run_id = ?
                """,
                (
                    state.value,
                    summary,
                    error_code,
                    safe_message,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.model_turns,
                    usage.tool_calls,
                    now,
                    now,
                    child_run_id,
                ),
            )
        return self.get_child_run(child_run_id)

    def mark_child_started(self, child_run_id: str) -> ChildRunRow:
        """记下子运行真正开始执行的时刻（幂等）。

        只在行仍是 running 且 `started_at IS NULL` 时写入：排队中的子运行必须
        保持 NULL——这是"排队中"与"真在跑"的唯一分界，取消/关停的判断都读它。
        重复调用不覆盖已有值（第一次开始的时刻才是真相）；行不存在抛
        NotFoundError（预留没落库属于真异常，不能当成"还没来得及开始"）。
        """
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"子运行不存在：{child_run_id}")
            if (
                row["state"] == ChildRunState.RUNNING.value
                and row["started_at"] is None
            ):
                connection.execute(
                    """
                    UPDATE delegation_child_runs
                    SET started_at = ?, updated_at = ?
                    WHERE child_run_id = ?
                    """,
                    (now, now, child_run_id),
                )
        return self.get_child_run(child_run_id)

    def mark_child_injected(self, child_run_id: str) -> ChildRunRow:
        """记下"这条子运行已经进过注入块"（幂等）。

        只在 `injected_at IS NULL` 时写入：终态子运行只注入一次，否则每轮重复
        历史会把父上下文的 token 吃爆；重复调用不覆盖第一次的时刻。行不存在抛
        NotFoundError（预留没落库属于真异常）。不校验状态：在跑的子运行同样可以
        打标，"要不要展示"由读这一行的注入规则决定。
        """
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"子运行不存在：{child_run_id}")
            connection.execute(
                """
                UPDATE delegation_child_runs
                SET injected_at = ?, updated_at = ?
                WHERE child_run_id = ? AND injected_at IS NULL
                """,
                (now, now, child_run_id),
            )
        return self.get_child_run(child_run_id)

    def mark_child_reported(self, child_run_id: str) -> ChildRunRow:
        """记下结果已投递回父会话（幂等），同时补上 `injected_at`。

        会话正文已经拿到摘要，所以投递成功时两枚标记一起置位，注入块不必再重复
        这条子运行；重复调用用 COALESCE 保住第一次的时刻。行不存在抛
        NotFoundError；行仍在 running 抛 InvalidStateError（结果还没产生，
        没有东西可以回流）。
        """
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"子运行不存在：{child_run_id}")
            if row["state"] == ChildRunState.RUNNING.value:
                raise InvalidStateError("只有终态的子运行可以汇报。")
            connection.execute(
                """
                UPDATE delegation_child_runs
                SET reported_at = COALESCE(reported_at, ?),
                    injected_at = COALESCE(injected_at, ?),
                    updated_at = ?
                WHERE child_run_id = ?
                """,
                (now, now, now, child_run_id),
            )
        return self.get_child_run(child_run_id)

    def get_child_run(self, child_run_id: str) -> Optional[ChildRunRow]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def find_child_run_by_spawn_key(
        self, *, parent_run_id: str, parent_tool_call_id: str
    ) -> Optional[ChildRunRow]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM delegation_child_runs
                WHERE parent_run_id = ? AND parent_tool_call_id = ?
                """,
                (parent_run_id, parent_tool_call_id),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_child_runs(self, parent_run_id: str) -> Tuple[ChildRunRow, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegation_child_runs
                WHERE parent_run_id = ?
                ORDER BY created_at, child_run_id
                """,
                (parent_run_id,),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def list_child_runs_for_conversation(
        self, parent_conversation_id: str
    ) -> Tuple[ChildRunRow, ...]:
        """该会话派过的全部子运行（含在跑的），按 created_at, child_run_id 排序。"""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegation_child_runs
                WHERE parent_conversation_id = ?
                ORDER BY created_at, child_run_id
                """,
                (parent_conversation_id,),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def list_unreported_terminal_child_runs(
        self, *, parent_conversation_id: Optional[str] = None
    ) -> Tuple[ChildRunRow, ...]:
        """终态且尚未汇报的子运行（唤醒要投递的队列）；可按会话过滤。

        `reported_at IS NULL` 让"已经回过父会话"的终态不再出现在队列里，
        进程重启也不会重复投递；按 terminal_at 排序保证先结束的先回流。
        """
        placeholders, terminal_values = _terminal_state_placeholders()
        clauses = [
            f"state IN {placeholders}",
            "reported_at IS NULL",
        ]
        parameters: list[object] = list(terminal_values)
        if parent_conversation_id is not None:
            clauses.append("parent_conversation_id = ?")
            parameters.append(parent_conversation_id)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delegation_child_runs"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY terminal_at, child_run_id",
                tuple(parameters),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def sweep_interrupted_child_runs(self) -> Tuple[ChildRunRow, ...]:
        """把残留的 running 行标成 interrupted（启动对账，幂等）。

        返回的是**改动后**重新读出的行：`state='interrupted'`、`terminal_at`
        非空，与 `SqliteTaskRunRepository.sweep_interrupted_runs` 的契约一致，
        调用方（启动对账）拿着这些行报告时不会把已中断的子运行误报成仍在跑。
        """

        now = self._clock()
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM delegation_child_runs WHERE state = ?",
                (ChildRunState.RUNNING.value,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE delegation_child_runs
                    SET state = ?, safe_message = ?, terminal_at = ?, updated_at = ?
                    WHERE child_run_id = ?
                    """,
                    (
                        ChildRunState.INTERRUPTED.value,
                        "进程重启，子运行未能收尾。",
                        now,
                        now,
                        row["child_run_id"],
                    ),
                )
        # 事务提交后重新读取，返回的是 interrupted 状态的行快照，而不是改动前的 running。
        return tuple(self.get_child_run(row["child_run_id"]) for row in rows)


def _terminal_state_placeholders() -> Tuple[str, Tuple[str, ...]]:
    """`IN (?,?,...)` 与它的参数：终态集合只认 `TERMINAL_CHILD_RUN_STATES`。

    这里刻意不把四个状态写成 SQL 字面量：那是一份会与 `TERMINAL_CHILD_RUN_STATES`
    漂移的第二份真相，漏改就等于"那一类终态永远不会被唤醒"。
    """
    values = tuple(sorted(state.value for state in TERMINAL_CHILD_RUN_STATES))
    return f"({','.join('?' for _ in values)})", values


def _from_row(row) -> ChildRunRow:
    return ChildRunRow(
        child_run_id=row["child_run_id"],
        parent_run_id=row["parent_run_id"],
        parent_tool_call_id=row["parent_tool_call_id"],
        parent_conversation_id=row["parent_conversation_id"],
        child_workspace_id=row["child_workspace_id"],
        depth=row["depth"],
        capability_profile=row["capability_profile"],
        workspace_mode=row["workspace_mode"],
        timeout_seconds=row["timeout_seconds"],
        task=row["task"],
        state=ChildRunState(row["state"]),
        summary=row["summary"],
        error_code=row["error_code"],
        safe_message=row["safe_message"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        model_turns=row["model_turns"],
        tool_calls=row["tool_calls"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        terminal_at=row["terminal_at"],
        updated_at=row["updated_at"],
        injected_at=row["injected_at"],
        reported_at=row["reported_at"],
    )


__all__ = ["SqliteDelegationChildRunRepository"]
