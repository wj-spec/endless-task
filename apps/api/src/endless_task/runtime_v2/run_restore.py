"""Run failure auto-restore observer (M3B slice B, 方案 A 兑现).

05 M3B slice A 把 run 级 checkpoint 接入主交互路径：run 首次写副作用前自动快照
工作区。本模块补上「失败即回滚」的半程——v2 run 到达终态 **FAILED**（执行故障：
provider 错误 / tool_execution_failed / runtime_internal_error / 各类安全停止）
时，自动把该 run 在工作区写下的 agent 改动恢复到 checkpoint。

规则（写死在文档与测试里，避免不可预测）：

- **只恢复 FAILED**：COMPLETED（run 正常收尾，保留结果）与 CANCELLED（用户主动
  停止——用户可能想保留部分状态）一律不恢复。
- **用户驱动的非终态不算失败**：approval DENY/EXPIRE 只是拒绝当次工具调用并让
  run 继续（execution.py:958 起），run 不会因此 FAILED；WAITING_APPROVAL 不是
  终态，也不触发。
- **守卫不变**（05 M3B slice A）：恢复只还原「账本 after_hash 与当前内容一致」的
  agent 改动；用户编辑过的文件、checkpoint 之后新建的文件一律跳过——绝不覆盖
  用户修改，也不删除任何文件。
- **无 checkpoint 的 run 是 no-op**（run 没有写过任何工作区文件）。

挂载：复用 AgentRunExecutor 的 ``trace_observer`` seam（terminal finally 内、硬超时
10s、fail-open），作为 fanout 的一个成员独立于 trace mode 组装——trace 关掉也生效。
进程内 coordinator 内存表不跨重启（05 M3B slice A 已记录边界），本观察者同局限。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalRunView:
    """What the auto-restore decision needs from the journal."""

    run_id: str
    status: str
    error_code: Optional[str] = None


def default_run_status_reader(repository) -> Callable[[str], TerminalRunView]:
    """Journal reader over a v2 repository (duck-typed, cycle-safe).

    ``repository`` needs ``get_run`` returning a record with ``id``,
    ``status`` (a value enum exposing ``.value``) and ``error_code``.
    """

    def read(run_id: str) -> TerminalRunView:
        run = repository.get_run(run_id)
        return TerminalRunView(
            run_id=run.id,
            status=run.status.value,
            error_code=run.error_code,
        )

    return read


class RunFailureAutoRestoreObserver:
    """Restore a failed run's workspace checkpoints (fail-open, idempotent).

    Errors are logged and swallowed — workspace rollback is a recovery aid
    and must never break run terminal handling (same contract as the trace
    observers). Applying twice is a no-op (current content already matches
    the checkpoint after the first restore).
    """

    def __init__(
        self,
        *,
        run_reader: Callable[[str], TerminalRunView],
        coordinator,
        excluded_codes: tuple[str, ...] = (),
        event_sink: Optional[Callable[[str, str, dict], None]] = None,
    ) -> None:
        self._run_reader = run_reader
        self._coordinator = coordinator
        self._excluded_codes = frozenset(excluded_codes)
        self._event_sink = event_sink

    async def on_run_terminal(self, run_id: str) -> None:
        try:
            view = self._run_reader(run_id)
            if view.status != "failed":
                return
            if view.error_code in self._excluded_codes:
                return
            roots = self._coordinator.workspace_roots_for(run_id=run_id)
            if not roots:
                # Run never wrote to a workspace: nothing to restore.
                return
            outcomes = self._coordinator.restore_run(run_id=run_id)
            for outcome in outcomes:
                logger.info(
                    "Failed run workspace auto-restored",
                    extra={
                        "run_id": run_id,
                        "workspace": outcome.workspace_root,
                        "restored": len(outcome.restored),
                        "skipped": len(outcome.skipped),
                        "errorCode": view.error_code,
                    },
                )
            if self._event_sink is not None and outcomes:
                # Audit trail on the run itself (surfaces later in UI/logs:
                # "该次失败的改动已自动回滚，用户修改未动").
                try:
                    self._event_sink(
                        run_id,
                        "run_auto_restored",
                        {
                            "errorCode": view.error_code,
                            "workspaces": len(outcomes),
                            "restored": sum(len(o.restored) for o in outcomes),
                            "skipped": sum(len(o.skipped) for o in outcomes),
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to record run_auto_restored event",
                        extra={"run_id": run_id},
                    )
        except Exception:
            logger.exception(
                "Run failure auto-restore failed",
                extra={"run_id": run_id},
            )


__all__ = [
    "RunFailureAutoRestoreObserver",
    "TerminalRunView",
    "default_run_status_reader",
]
