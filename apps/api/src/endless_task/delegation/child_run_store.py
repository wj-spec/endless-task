"""委派子运行记录的存储契约（切片 9）。

为什么这一层单独存在：`delegation/*.py` 不允许导入 `endless_task.storage`
（架构测试的 PROTOCOL_FILES 纪律），所以协调器只认这个**鸭子类型协议**，
SQLite 实现放在 storage 层并反向 import 这里的类型。

`ChildRunState` 与既有的两个状态枚举都不同：`ChildRunStatus`（coordinator.py）
与 `ChildStatus`（protocol.py）**都没有"运行中"**——异步之后"在跑"是常态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Tuple

from .protocol import UsageSummary


class ChildRunState(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 进程死在半路：启动对账把残留的 running 行改成它，避免永远"在跑"。
    INTERRUPTED = "interrupted"


TERMINAL_CHILD_RUN_STATES = frozenset(
    {
        ChildRunState.COMPLETED,
        ChildRunState.FAILED,
        ChildRunState.CANCELLED,
        ChildRunState.INTERRUPTED,
    }
)


@dataclass(frozen=True)
class ChildRunRow:
    """一行子运行记录（进行中或终态）。"""

    child_run_id: str
    parent_run_id: str
    parent_tool_call_id: str
    parent_conversation_id: str
    child_workspace_id: Optional[str]
    depth: int
    capability_profile: str
    workspace_mode: str
    timeout_seconds: float
    task: str
    state: ChildRunState
    summary: Optional[str]
    error_code: Optional[str]
    safe_message: Optional[str]
    input_tokens: int
    output_tokens: int
    model_turns: int
    tool_calls: int
    created_at: str
    started_at: Optional[str]
    terminal_at: Optional[str]
    updated_at: str
    #: 最后一次被写进 <delegated-children> 注入块的时刻（终态只注入一次）。
    injected_at: Optional[str] = None
    #: 结果已投递回父会话的时刻（唤醒链路用；终态且未汇报才需要回流）。
    reported_at: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_CHILD_RUN_STATES


class ChildRunStore(Protocol):
    """协调器看到的子运行存储面。"""

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
        """插入一行 running；同一 spawn key 已存在时返回 None（幂等拒绝）。"""
        ...

    def mark_child_terminal(
        self,
        child_run_id: str,
        *,
        state: ChildRunState,
        summary: Optional[str] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        usage: Optional[UsageSummary] = None,
    ) -> ChildRunRow: ...

    def mark_child_started(self, child_run_id: str) -> ChildRunRow:
        """记下子运行**真正开始执行**的时刻（幂等）。

        `started_at` 是"还在并发闸门外排队"与"已经在内核里跑"的分界，取消与关停
        都读它。因此只在行仍是 `running` 且 `started_at IS NULL` 时写入；已有值
        一律不覆盖（重复调用无害）。行不存在抛 `NotFoundError`。
        """
        ...

    def get_child_run(self, child_run_id: str) -> Optional[ChildRunRow]: ...

    def find_child_run_by_spawn_key(
        self, *, parent_run_id: str, parent_tool_call_id: str
    ) -> Optional[ChildRunRow]: ...

    def list_child_runs(self, parent_run_id: str) -> Tuple[ChildRunRow, ...]: ...

    def mark_child_injected(self, child_run_id: str) -> ChildRunRow:
        """记下"这条子运行已经进过注入块"；已有时间戳时不覆盖（幂等）。"""
        ...

    def mark_child_reported(self, child_run_id: str) -> ChildRunRow:
        """记下"结果已投递回父会话"；同时补 injected_at（会话正文已有摘要）。

        行不存在 → NotFoundError；行仍是 running → InvalidStateError。
        """
        ...

    def list_child_runs_for_conversation(
        self, parent_conversation_id: str
    ) -> Tuple[ChildRunRow, ...]:
        """该会话派过的全部子运行，按 created_at, child_run_id 排序。"""
        ...

    def list_unreported_terminal_child_runs(
        self, *, parent_conversation_id: Optional[str] = None
    ) -> Tuple[ChildRunRow, ...]:
        """终态且尚未汇报的子运行（唤醒要投递的队列）；可按会话过滤。"""
        ...

    def sweep_interrupted_child_runs(self) -> Tuple[ChildRunRow, ...]:
        """把残留的 running 行标成 interrupted（启动对账，幂等）。

        返回的是**改动后**的行：`state='interrupted'`、`terminal_at` 非 None，
        调用方拿它们报告时不会把已中断的子运行误报成仍在跑。
        """
        ...


__all__ = [
    "TERMINAL_CHILD_RUN_STATES",
    "ChildRunRow",
    "ChildRunState",
    "ChildRunStore",
]
