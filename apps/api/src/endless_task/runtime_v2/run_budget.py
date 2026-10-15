"""运行预算：一次运行的时长与成本上限，按运行类别取值。

为什么不沿用单一硬墙：120 秒对"用户盯着的一轮对话"是合理护栏，对"定时任务在
后台把一件事推进完"则是硬伤——复杂任务根本跑不完。

soft_seconds：到达即**提请人工决策**，但**不中断**运行（复用 C4 升级通道）。
hard_seconds：到达即**优雅收尾**——落阶段性结论 + 引导语，运行记 COMPLETED。
              长活到点应该留下可继续的成果，而不是 FAILED。
per_turn_seconds：单次模型调用上限。墙体变大后，一次挂死的调用会吃掉整段预算，
              所以每轮必须另有活性上限。
stop_on_cost：成本到顶时是**停**（真）还是只**提请**（假）。父档恒为假（今天的
              行为），子运行恒为真（见 `child_run_budget`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

#: 单次模型调用上限（秒）。与 provider 超时同量级。
DEFAULT_PER_TURN_SECONDS = 60.0


@dataclass(frozen=True)
class RunBudget:
    soft_seconds: Optional[float] = None
    hard_seconds: Optional[float] = None
    cost_cap_usd: float = 0.0
    per_turn_seconds: float = DEFAULT_PER_TURN_SECONDS
    #: 成本到顶要**停止**运行（真）还是只提请人工决策（假，默认 = 今天的行为）。
    #: 为什么做成预算值对象上的策略位而不是执行器的构造参数：`AgentRunExecutor`
    #: 的构造签名是冻结面，而"按运行类别取策略"本来就该跟预算一起走。
    stop_on_cost: bool = False

    def __post_init__(self) -> None:
        for name in ("soft_seconds", "hard_seconds"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if (
            self.soft_seconds is not None
            and self.hard_seconds is not None
            and self.soft_seconds >= self.hard_seconds
        ):
            raise ValueError("soft_seconds must be smaller than hard_seconds")
        if self.cost_cap_usd < 0:
            raise ValueError("cost_cap_usd must be non-negative")
        if self.per_turn_seconds <= 0:
            raise ValueError("per_turn_seconds must be positive")

    def reached_soft(self, elapsed_seconds: float) -> bool:
        """是否已到"提请人工决策"的时长阈值（不中断运行）。"""
        return self.soft_seconds is not None and elapsed_seconds >= self.soft_seconds

    def remaining_hard(self, elapsed_seconds: float) -> Optional[float]:
        """距硬上限还剩多少秒（未配置硬上限时为 None）。"""
        if self.hard_seconds is None:
            return None
        return self.hard_seconds - elapsed_seconds

    def turn_timeout(self, elapsed_seconds: float) -> float:
        """本轮模型调用允许的最长时间：min(剩余硬预算, 单轮上限)。"""
        remaining = self.remaining_hard(elapsed_seconds)
        if remaining is None:
            return self.per_turn_seconds
        return max(0.0, min(remaining, self.per_turn_seconds))


#: 交互式对话：用户盯着看，软阈值给"仍在进行"的信号，硬阈值兜底 15 分钟。
INTERACTIVE_BUDGET = RunBudget(
    soft_seconds=180.0, hard_seconds=900.0, cost_cap_usd=1.0
)
#: 定时/手动任务运行：无人盯着，软阈值走通知，硬阈值兜底 60 分钟。
TASK_BUDGET = RunBudget(
    soft_seconds=600.0, hard_seconds=3600.0, cost_cap_usd=3.0
)
#: 显式长跑：只有被显式选中才用（本期只经环境变量启用，未做用户开关）。
LONG_BUDGET = RunBudget(
    soft_seconds=3600.0, hard_seconds=14400.0, cost_cap_usd=10.0
)

DEFAULT_BUDGETS = {
    "interactive": INTERACTIVE_BUDGET,
    "task": TASK_BUDGET,
    "long": LONG_BUDGET,
}

#: 子运行的硬墙下限：模型把 timeoutSeconds 填得很小或时间已过时，仍给一次收尾机会。
CHILD_MIN_HARD_SECONDS = 1.0

#: 子运行的硬墙**上限**默认值（秒）：与任务档 hard 同量级。
#: `SpawnSpec.timeout_seconds` 收得到 86400（一天），不设上限时模型一句"跑一天"
#: 就能占着并发名额与在册配额一整天。钳制发生在**派生时**（调用方把这个值传给
#: `child_run_budget`），执行器拿到的仍是"还剩多少"。
CHILD_DEFAULT_MAX_HARD_SECONDS = 3600.0

#: 子运行的成本上限默认值（美元）：与任务档同价（$3）。子运行**默认就有**真上界；
#: 想关掉就显式配 `ENDLESS_TASK_MAX_CHILD_COST_USD=0`（cap=0 → 闸门惰性）。
#:
#: **如实前提**：这道闸要**模型可定价**才会触发——`estimate_cost_usd` 对未定价的
#: provider/model 返回 None，`cost_cap_exceeded` 于是恒为 False。未定价时"配置位
#: 存在但拦不住"，这里不假装它一定生效。
CHILD_DEFAULT_COST_CAP_USD = 3.0


def child_run_budget(
    deadline_monotonic: Optional[float],
    *,
    now: Optional[float] = None,
    max_hard_seconds: Optional[float] = None,
    cost_cap_usd: Optional[float] = None,
) -> RunBudget:
    """按 `RunCommand.deadline_monotonic` 给子运行算一份预算。

    委派协议里的 `SpawnSpec.timeout_seconds` 一直是**写了没人读**的字段
    （协调器把它写成 deadline，适配器从不读）。子运行转后台之后，它必须成为
    唯一的墙，否则子运行会退化成"能跑任务档 3600 秒、还占着并发名额"的东西。

    不设软阈值：子运行跑在隐藏会话里，"到阈值请人工决策"对它没有意义，
    硬墙上的优雅收尾才是要的护栏。

    `max_hard_seconds` 是本次派生的墙上限（来自设置 `max_child_hard_seconds`），
    用来钳制模型声明的超长 timeout：`hard = max(CHILD_MIN_HARD_SECONDS,
    min(remaining, max_hard_seconds))`。**下限优先于上限**——上限若被配得比下限
    （1 秒）还小，子运行仍要拿到一次收尾机会，而不是被压成 0 秒。传 None（默认）
    时不设上限，与加这个参数之前逐字一致。

    `cost_cap_usd` 是本次派生的成本上限（来自设置 `max_child_cost_usd`）：
    - 传非零值 → `cost_cap_usd=<值>`（真上界）；
    - 传 None（默认）或 0 → 保持 `cost_cap_usd=0.0`，与加这个参数之前逐字一致
      （cap=0 时 `cost_cap_exceeded` 恒 False，闸门惰性）。
    派生预算**恒**带 `stop_on_cost=True`：子运行无人盯着，"只提请不停止"没有意义，
    所以只要真配了 cap 就必须停。父档（`TASK_BUDGET` 回退）不带这个策略位——
    那里是与定时任务共用的一档，父档行为必须逐字节不变。
    """
    if deadline_monotonic is None:
        # 时间档（`deadline_monotonic is None` → TASK_BUDGET）不走这条上限：
        # 上限只约束"模型声明出来的"子运行墙。成本上限同理——回退档是与定时任务
        # 共用的预算对象，不能在这里给它加上"到顶就停"的策略（父档行为会变）。
        return TASK_BUDGET
    reference = time.monotonic() if now is None else now
    remaining = float(deadline_monotonic) - reference
    capped = remaining if max_hard_seconds is None else min(remaining, max_hard_seconds)
    if capped < CHILD_MIN_HARD_SECONDS:
        capped = CHILD_MIN_HARD_SECONDS
    return RunBudget(
        soft_seconds=None,
        hard_seconds=capped,
        cost_cap_usd=float(cost_cap_usd) if cost_cap_usd else 0.0,
        per_turn_seconds=DEFAULT_PER_TURN_SECONDS,
        stop_on_cost=True,
    )


__all__ = [
    "CHILD_DEFAULT_COST_CAP_USD",
    "CHILD_DEFAULT_MAX_HARD_SECONDS",
    "CHILD_MIN_HARD_SECONDS",
    "DEFAULT_BUDGETS",
    "DEFAULT_PER_TURN_SECONDS",
    "INTERACTIVE_BUDGET",
    "LONG_BUDGET",
    "RunBudget",
    "TASK_BUDGET",
    "child_run_budget",
]
