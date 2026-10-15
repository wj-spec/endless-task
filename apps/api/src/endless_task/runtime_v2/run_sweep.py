"""切片 10 T2 Step 3：v2 run 的启动清扫（死 run 不许再假装在跑）。

**为什么需要它**：task run 有 `SqliteTaskRunRepository.sweep_interrupted_runs`、
委派子运行有 `SqliteDelegationChildRunRepository.sweep_interrupted_child_runs`，
**v2 run 一张表没有清道夫**。进程被杀之后，v2 run 行永久停在
`created/queued/running/waiting_approval/compacting/cancelling`：

- 快照的 `runningRunId` 由**数据库状态**推导（`runtime_v2/replay.py`
  `build_conversation_snapshot`），不是进程内活动表 → 前端拿到"还在跑"；
- 同一时刻 `gateway.has_active_run()` 为假 → SSE 流立刻收流
  （`routes/v2_conversations.py`）→ 前端每 800ms 重连一次、永远重连下去，
  界面上写着"事件流重连中"，而这一 run 其实已经死了。

**为什么是启动清扫而不是"快照带活性判定"**（控制器裁定 R3 选 (a)）：活性判定要
在快照里引入第二份真相（进程内活动表），而清扫只用既有语义把不可持有的行落终态，
零迁移、零新字段、对三类 run 一视同仁；顺带把"崩溃后的恢复卡片"从
`interruptedRuns` 的持续假象变成一条**真的失败**（`run_failed` 事件 + 如实文案）。

**不许误伤在跑的 run**——两条结构性保证：

1. 调用点只有一处：API lifespan 的启动段（`api/app.py`），`yield` 之前，
   进程还没有服务过任何请求；没有任何后台任务会周期性调用它。
2. 作用域带时间边界：只清 `created_at` **早于**清扫时刻的行。本进程启动之后
   开始的 run（`created_at` 更晚）永远不在范围内——即使有人在运行期误调用
   这个函数，也伤不到在跑的 run（守卫用例钉住这一点，见
   `tests/test_run_sweep.py`）。

**前提：单进程**（切片 10 终审修复 W-I，显式写进代码而不是靠"今天碰巧是"）。
整条推理链只在本 API 进程**独占**这张库时成立：`api/main.py` 的 uvicorn
未设 `workers`，也没有第二个写入方。若哪天起了多 worker（或多实例共享同一个
SQLite 文件），每个 worker 启动都会清一次，而**别的 worker 正在跑的 run**
在它眼里就是"本进程不可能持有"的活动态行 → 会被误判成因重启中断而落 FAILED，
在跑的 run 则继续写自己的终态（状态互相打架，产物与错误码都不是真的）。
时间边界挡不住这种误伤：别的 worker 的 run 是在本进程之前创建的，`created_at`
本来就早于本次清扫时刻。那时唯一诚实的做法是给清扫加真正的跨进程租约/属主字段
（= 迁移），**本切片不做**，所以引入多 worker 之前必须先解决这一点。

这条前提不是靠注释维持的：`SINGLE_PROCESS_ASSUMPTION` 是机器可读的声明，
`tests/test_run_sweep.py` 的守卫用例同时钉住"启动段只起一个进程/worker"
与"本模块只有那一处调用点"——任一条被破坏（例如给 uvicorn 加 `workers=4`）
该用例即红。

**零迁移**：只 UPDATE `v2_runs`（及其既有子表语义），没有列变更。已开始的行
走既有 `finalize_interrupted_run`（与用户在恢复卡片上点"结束本次运行"**同一条**
路径）；从未开始的行（`started_at` 为空）走 `transition_run_status(FAILED)`，
`started_at` 保持 NULL——**绝不用"现在"伪造一个开始时刻**。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .domain import RunRecord, RunStatus
from ..storage.v2.base import ACTIVE_RUN_STATUSES

logger = logging.getLogger(__name__)

#: 清扫落终态用的错误码：让"进程重启中断"这条事实可被检索，而不是一句泛化文案。
SWEEP_ERROR_CODE = "v2_process_restart"
#: 已开始的 run 的如实文案（与 task run 清扫的口径一致：说清"是谁中断了它"）。
SWEEP_SAFE_MESSAGE = "进程重启中断了这次运行。"
#: 从未开始的 run 的如实文案（它没有产物，也没有开始时刻）。
SWEEP_NEVER_STARTED_MESSAGE = "进程重启，这次运行从未开始。"

#: 切片 10 终审修复 W-I：单进程前提的**机器可读**声明。
#: `tests/test_run_sweep.py` 的守卫用例断言启动段（`api/main.py` → `api/app.py`）
#: 确实只起一个进程/worker，并且本模块只有那一处调用点；若哪天有人给 uvicorn
#: 加上 `workers=N>1`（或引入第二个调用点），那条用例会红。
SINGLE_PROCESS_ASSUMPTION = (
    "启动清扫只在『本进程独占这张库』时成立：多 worker 会互删在跑的 run"
)


def _moment(value: object) -> Optional[datetime]:
    """解析 ISO 时间戳；解析不了返回 None（宁可漏扫，也不误伤）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def sweep_interrupted_v2_runs(
    repository: Any,
    *,
    started_before: Optional[str] = None,
) -> tuple[RunRecord, ...]:
    """把"本进程不可能持有"的 v2 run 落终态，返回**改动后**重新读出的行。

    `started_before` 是本次清扫的时间边界（默认取调用时刻 = 进程启动时刻）。
    只有 `created_at` 早于它的活动态 run 会被清扫；时间戳解析不了的行走保守
    分支（跳过 + 日志），绝不因为"看起来像旧的"就动它。

    幂等：终态行不在 `ACTIVE_RUN_STATUSES` 里，第二次调用是空操作。逐行容错：
    单行被并发改到终态（`InvalidStateError`）只跳过该行，不拖垮启动。
    """
    cutoff = _moment(started_before) if started_before is not None else None
    if cutoff is None:
        # 默认边界 = 调用时刻。生产调用点只有 API lifespan 的启动段（还没有服务过
        # 任何请求），所以"调用时刻"就是"本进程启动时刻"。
        cutoff = datetime.now(timezone.utc)
    swept: list[RunRecord] = []
    active_runs = repository.list_runs(statuses=tuple(ACTIVE_RUN_STATUSES))
    for run in active_runs:
        created = _moment(run.created_at)
        if created is None:
            logger.warning(
                "Startup sweep skipped run with unparseable created_at",
                extra={"run_id": run.id, "created_at": run.created_at},
            )
            continue
        if created >= cutoff:
            # 本进程启动之后才出现的行：不属于"上一个进程的遗物"，不动。
            continue
        try:
            if run.started_at:
                swept.append(
                    repository.finalize_interrupted_run(
                        run.id,
                        error_code=SWEEP_ERROR_CODE,
                        safe_message=SWEEP_SAFE_MESSAGE,
                    )
                )
            else:
                # 从未开始：没有产物、没有开始时刻，落 FAILED 但 started_at 保持
                # NULL（`finalize_interrupted_run` 会 COALESCE 成"现在"，那是伪造）。
                swept.append(
                    repository.transition_run_status(
                        run.id,
                        RunStatus.FAILED,
                        event_type="run_failed",
                        payload={
                            "errorCode": SWEEP_ERROR_CODE,
                            "safeMessage": SWEEP_NEVER_STARTED_MESSAGE,
                        },
                        error_code=SWEEP_ERROR_CODE,
                        safe_message=SWEEP_NEVER_STARTED_MESSAGE,
                    )
                )
        except Exception:  # noqa: BLE001 单行失败不影响其余行与启动
            logger.exception(
                "Startup sweep failed for run",
                extra={"run_id": run.id, "status": run.status.value},
            )
    if swept:
        logger.warning(
            "Startup sweep finalized interrupted v2 runs",
            extra={
                "count": len(swept),
                "runIds": [run.id for run in swept],
            },
        )
    return tuple(swept)


__all__ = [
    "SINGLE_PROCESS_ASSUMPTION",
    "SWEEP_ERROR_CODE",
    "SWEEP_NEVER_STARTED_MESSAGE",
    "SWEEP_SAFE_MESSAGE",
    "sweep_interrupted_v2_runs",
]
