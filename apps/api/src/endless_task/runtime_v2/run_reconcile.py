"""Crash-window startup reconciliation (M3B slice E).

方案 B/D 之后自动恢复只覆盖「进程活着到达终态」的失败：executor 在 run 终态
``finally`` 里同步 restore（05 M3B slice B）。若进程在 run 已 FAILED 但 restore
执行前崩溃/被杀，重启后该 run 仍是 FAILED 且无 ``run_auto_restored`` 审计事件——
没有任何触发器补做恢复（05 M3B slice B/C 边界第 1 条）。

本模块在 API 启动段对账：

- 候选 = 终态 FAILED 的 run，**最新优先**（finished_at 降序）；
- 逐个跳过：已有 ``run_auto_restored`` 事件（进程内已恢复 / 对账已做过）→ 跳过；
  coordinator 无该 run 的 checkpoint ref（从未写过工作区）→ 跳过；workspace 目录
  已不存在（用户删了工作区）→ 跳过；
- 对剩余 run 的每个存在 workspace 执行 ``apply_restore``——守卫/逐 run 归属沿用
  slice A/B/D：只还原该 run 自己的 intact agent 改动，绝不覆盖用户修改、后序 run
  改动与 cp 后新建文件；
- 每个有 cp 且 workspace 存在的 run 在 restore 后写 ``run_auto_restored`` 事件
  （trigger=startup_reconcile）——幂等终止标记（即便 restored=0，避免下次启动重复
  尝试）。

与 ``RunFailureAutoRestoreObserver`` 同纪律：整体 fail-open，错误只日志，绝不阻塞
API 启动。复用 ``ENDLESS_TASK_RUN_AUTO_RESTORE`` 闸门（默认 1）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from endless_task.runtime_v2 import RunStatus

logger = logging.getLogger(__name__)

#: marker written by the in-process observer AND by this reconciler.
_RESTORED_EVENT = "run_auto_restored"


@dataclass(frozen=True)
class ReconciledRun:
    run_id: str
    restored: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileSummary:
    candidates: int
    restored_runs: int
    restored_files: int
    skipped_files: int


async def reconcile_failed_runs(
    *,
    repository,
    coordinator,
) -> ReconcileSummary:
    """Restore FAILED runs whose restore never ran (crash window).

    ``repository`` needs ``list_runs(statuses=...)`` returning run records with
    ``id``/``status``/``finished_at``/``created_at``, ``list_runtime_events``
    returning records with ``event_type``, and ``append_runtime_event``.
    ``coordinator`` is the RunCheckpointCoordinator (durable refs from slice C).
    """
    failed_runs = list(
        repository.list_runs(statuses=(RunStatus.FAILED,))
    )
    ordered = sorted(
        failed_runs,
        key=lambda run: (run.finished_at or run.created_at or ""),
        reverse=True,
    )
    reconciled: list[ReconciledRun] = []
    for run in ordered:
        run_id = run.id
        try:
            events = repository.list_runtime_events(run_id)
            if any(event.event_type == _RESTORED_EVENT for event in events):
                continue
            roots = coordinator.workspace_roots_for(run_id=run_id)
            if not roots:
                continue
            existing_roots = tuple(
                root for root in roots if Path(root).expanduser().is_dir()
            )
            if not existing_roots:
                continue
            restored: list[str] = []
            skipped: list[str] = []
            for root in existing_roots:
                restored_for_root, skipped_for_root = coordinator.apply_restore(
                    run_id=run_id, workspace_root=root
                )
                restored.extend(restored_for_root)
                skipped.extend(skipped_for_root)
            try:
                repository.append_runtime_event(
                    run_id=run_id,
                    event_type=_RESTORED_EVENT,
                    payload={
                        "errorCode": run.error_code,
                        "workspaces": len(existing_roots),
                        "restored": len(restored),
                        "skipped": len(skipped),
                        "trigger": "startup_reconcile",
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to record startup reconcile event",
                    extra={"run_id": run_id},
                )
            reconciled.append(
                ReconciledRun(
                    run_id=run_id,
                    restored=tuple(restored),
                    skipped=tuple(skipped),
                )
            )
            logger.info(
                "Startup reconcile restored failed run",
                extra={
                    "run_id": run_id,
                    "restored": len(restored),
                    "skipped": len(skipped),
                    "errorCode": run.error_code,
                },
            )
        except Exception:
            logger.exception(
                "Startup reconcile failed for run",
                extra={"run_id": run_id},
            )
    return ReconcileSummary(
        candidates=len(ordered),
        restored_runs=len(reconciled),
        restored_files=sum(len(run.restored) for run in reconciled),
        skipped_files=sum(len(run.skipped) for run in reconciled),
    )


__all__ = [
    "ReconciledRun",
    "ReconcileSummary",
    "reconcile_failed_runs",
]
