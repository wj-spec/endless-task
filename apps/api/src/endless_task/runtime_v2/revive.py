"""E3/B：**硬停后的终态 run 原地复活**（同一条 run 接着跑）的判据与编排。

为什么需要一个新模块而不是给冻结类加方法
========================================

`storage_method_inventory.json` 冻结了 16 个类的**方法名 + 签名文本**；
`module_surface_inventory.json` 冻结了 `runtime_v2.execution` / `runtime_v2.gateway` /
`workspace_runtime.fs_tools` 三个模块的**模块级名字**。恢复实现只需
**新增一个仓储方法**（`SqliteRuntimeV2Repository.revive_terminal_run`，清单 83→84），
并同提交重生 `storage_method_inventory.json`。

⇒ 复活的**判据**与**拒绝原因**放在这里：本模块是 `runtime_v2` 下的普通模块，
既不在被冻结的三个模块里，模块级函数也不进"只收类方法"的方法清单（`run_progress.py`
的先例）。网关（`gateway_impl/runs.py`）与快照投影（`gateway_impl/base.py`）都只
**调用**它，不新增/改名任何既有方法，也不新增路由。

复活是什么、不是什么
====================

* **是**：把一条因**预算硬停**（时间到墙 / 成本到顶）而落在
  ``COMPLETED + awaiting_user(willStop=true)`` 的 run，**原地**改回 ``running``，
  并从 E1 的进度读取器认定的断点继续 ⇒ 用户看到的是"**同一条 run** 接着跑"。
* **不是**：新开一轮（那会换 run 身份，`regenerate_run` / `resend_run` 已覆盖），
  也不是崩溃中断 run 的恢复通道（那条走 ``resolve_recovery`` 既有的
  `finalize_interrupted_run` + 新 run 语义，本模块不碰）。

四条否定判据（任一不满足 ⇒ **指名拒绝**，绝不靠数据库报错兜底）
============================================================

=====  ==========================  ====================================================
判据    拒绝原因码                  为什么
=====  ==========================  ====================================================
①      ``run_not_completed``       `FAILED` / `CANCELLED` / 仍活动态：没有"接着跑"这回事
②      ``no_hard_stop``            没有 `willStop=true` 的 `run.awaiting_user`（不是硬停形态）
③      ``uncertain_side_effects``  有"结果无法确认"的工具体（E2 通道负责；复活会**立刻**
                                   被 E2 的收尾路径再次停住 ⇒ 复活等于空转）
④      ``lane_has_other_active_run``  该 lane 上另有活动 run（`036` 的部分唯一索引是
                                   **lane 层**的；原地复活本身不违反它，但这条前提必须先确认）
=====  ==========================  ====================================================

③ 为什么必须在这里挡（而不是留给执行器）：E2 的收尾会把"崩溃/中断时处于活动态"的
工具体**故意留在活动态**，而执行器进入循环前**第一件事**就是看 `run_progress.uncertain_tools`
（`runner.py` 的 E2 分支）⇒ 这样的 run 复活后**一次模型调用都不会发生**、立刻再次落
`COMPLETED + awaiting_user`。对用户就是"点了继续、什么都没有发生、横幅又回来了"。
⇒ 这一形态的出路是 E2 的**用户决定通道**（升级面），不是复活。

三条"活耗时/用量"口径（ADR §3.2 / §3.4，落地处见 `storage/v2/runs.py` 的同名方法）
=================================================================================

* **复活视为新的一段**：`started_at` 被改写成复活时刻 ⇒ `elapsedSeconds` /
  `budget.usedSeconds` 只数**新的一段**，**停机时间不计入"已用"**；
* 旧的一段（`previousStartedAt` / `previousFinishedAt` / `previousSegmentSeconds`）
  写进 `run_progress_resumed` 事件的 payload —— **不假装总预算连续**，但也不丢掉事实；
* 用量（`v2_runs` 没有用量列）只能按 `v2_model_turns` 求和：复活**不重置**已完成轮的
  用量（E1 的 `RunProgress` 会把它接上），而**新的一段**由执行器再发一条
  `run_budget_declared` 重新声明（快照取最后一条 ⇒ 剩余秒数按新段算）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .domain import RunStatus, ToolExecutionRecord
from .execution_impl.run_progress import is_uncertain_tool_execution

#: 拒绝原因码（稳定、可断言；同时出现在 `RunNotRevivableError.code` 的 message 里）。
REFUSAL_RUN_NOT_COMPLETED = "run_not_completed"
REFUSAL_NO_HARD_STOP = "no_hard_stop"
REFUSAL_UNCERTAIN_SIDE_EFFECTS = "uncertain_side_effects"
REFUSAL_LANE_HAS_OTHER_ACTIVE_RUN = "lane_has_other_active_run"

#: 复活发出的**同 run 恢复事件**：与 C2"卡住后恢复"复用同一个事件类型
#: （`runner._append_progress_resumed`）⇒ 前端两条横幅（升级/卡住）都被它清掉。
#: ADR §3.1 明确要求复用并写明"语义合流"：对消费者而言它只有一个含义——
#: "**这条 run** 又活过来了，之前那份'等你决定'不再成立"。
REVIVE_EVENT_TYPE = "run_progress_resumed"

#: 活动 run 状态（与 `storage/v2/base.py` 的 `ACTIVE_RUN_STATUSES`、
#: `036_runtime_v2.sql` 的部分唯一索引**逐字一致**）。这里不 import 存储层：
#: `runtime_v2` 不依赖 `endless_task.storage`（`gateway_impl/base.py` 的
#: `_require_no_active_runs` 也是就地列出，同一姿势）。
_ACTIVE_RUN_STATUSES = (
    RunStatus.CREATED,
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL,
    RunStatus.COMPACTING,
    RunStatus.CANCELLING,
)


@dataclass(frozen=True)
class ReviveDecision:
    """"这条 run 能不能原地复活"的判定结果（纯数据，可直接进快照/日志）。"""

    revivable: bool
    reason: Optional[str] = None
    detail: str = ""

    @property
    def refusal_message(self) -> str:
        """指名拒绝的完整文案：`<原因码>：<人话>`（HTTP 409 的 message 就是它）。"""
        if self.revivable:
            return ""
        return f"{self.reason}：{self.detail}"


def evaluate_revive(
    *,
    run_status: RunStatus,
    hard_stop: Optional[bool],
    uncertain_tool_execution_ids: Sequence[str] = (),
    other_active_run_ids: Sequence[str] = (),
) -> ReviveDecision:
    """纯函数：四个**显式输入** ⇒ 判定。不读库、不读时钟、无副作用。

    * ``run_status``：run 行自己的状态；
    * ``hard_stop``：该 run 的**最后**一条 `run_awaiting_user` 是不是
      `willStop=true`（没有这类事件 ⇒ ``None``）；
    * ``uncertain_tool_execution_ids``：结果无法确认的工具体
      （``run_progress.is_uncertain_tool_execution`` 的口径）；
    * ``other_active_run_ids``：**同 lane** 上别的活动 run。
    """
    if run_status is not RunStatus.COMPLETED:
        return ReviveDecision(
            False,
            REFUSAL_RUN_NOT_COMPLETED,
            f"只有硬停后 COMPLETED 的 run 可以原地复活（当前状态：{run_status.value}）",
        )
    if hard_stop is not True:
        return ReviveDecision(
            False,
            REFUSAL_NO_HARD_STOP,
            "这条 run 没有 willStop=true 的升级（不是硬停形态，没有可接续的断点）",
        )
    if uncertain_tool_execution_ids:
        return ReviveDecision(
            False,
            REFUSAL_UNCERTAIN_SIDE_EFFECTS,
            "有工具执行的结果无法确认（"
            + "、".join(uncertain_tool_execution_ids)
            + "）：请先在升级面决定怎么办，复活不会重放它们",
        )
    if other_active_run_ids:
        return ReviveDecision(
            False,
            REFUSAL_LANE_HAS_OTHER_ACTIVE_RUN,
            "这条 lane 上另有活动运行（" + "、".join(other_active_run_ids) + "）",
        )
    return ReviveDecision(True)


def last_awaiting_user_hard_stop(events: Sequence[Any]) -> Optional[bool]:
    """该 run **最后**一条 `run_awaiting_user` 是否 `willStop=true`。

    取"最后一条"而不是"任意一条"：硬停收尾把它自己那条 `willStop=true` 放在最后
    （`_finalize_time_budget` / E2 收尾都是"先升级、后定案"），所以"最后一条"就是
    用户此刻看到的那条横幅。软阈值提醒（`willStop=false`）后正常跑完的 run 不算硬停。
    """
    latest: Optional[bool] = None
    for event in events:
        if getattr(event, "event_type", None) != "run_awaiting_user":
            continue
        payload = getattr(event, "payload", None)
        latest = bool(isinstance(payload, dict) and payload.get("willStop"))
    return latest


def collect_revive_decision(*, repository: Any, run_id: str) -> ReviveDecision:
    """读库版：把 `evaluate_revive` 的四个输入取齐（唯一的 I/O 入口）。

    姿势照 `execution_impl/run_progress.py` 的 `collect_run_progress`：它负责**取值**，
    判定仍然是纯函数。快照投影（硬停横幅在场时）与复活入口都用它，保证
    "前端看到可复活"与"点下去真的能复活"同源。
    """
    run = repository.get_run(run_id)
    events = repository.list_runtime_events(run_id)
    uncertain_ids: list[str] = []
    for turn in repository.list_model_turns(run_id):
        for record in repository.list_tool_executions(turn.id):
            if is_uncertain_tool_execution(record):
                uncertain_ids.append(record.id)
    other_active_ids = [
        other.id
        for other in repository.list_runs(
            conversation_id=run.conversation_id,
            statuses=_ACTIVE_RUN_STATUSES,
        )
        if other.lane_id == run.lane_id and other.id != run_id
    ]
    return evaluate_revive(
        run_status=run.status,
        hard_stop=last_awaiting_user_hard_stop(events),
        uncertain_tool_execution_ids=uncertain_ids,
        other_active_run_ids=other_active_ids,
    )
