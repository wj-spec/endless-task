"""C4 终止与升级：无进展 / 预算将尽时把决定权交回人类。

《Agentic AI Guide》第 27 章「循环工程 — 终止工程」给出两类终止条件与一条
升级路径：

* ``no_progress_detected → persist stuck → escalate_to_human``
* ``budget.exhausted → escalate_to_human``

本模块只负责**把"该由人来决定"这件事说清楚**：判定预算是否将尽、把当前进展
（做到哪一步）、卡点（失败记忆）和可选项（继续 / 换路径 / 人工接管）压成一份
``EscalationReport``。它不做 I/O、不看时钟、不改运行状态——状态机与事件由
执行循环负责，前端只消费这份报告。

边界（有意为之）：升级不是"暂停运行"。报告里 ``will_stop`` 说明安全停止策略
是否已经/将要结束本次运行；用户的三条出路都映射到已有可靠动作——
继续/换路径走 steer 通道，人工接管走取消通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

ESCALATION_PROTOCOL_VERSION = 1

#: 无进展（连续同因失败 / no-progress 评估）触发的升级。
NO_PROGRESS_REASON = "no_progress"

#: 预算将尽（上下文用量接近上限且未压缩）触发的升级。
BUDGET_REASON = "budget_exhausted"

#: C1 独立验证不通过触发的升级。
VERIFICATION_REASON = "verification_failed"

#: C5 成本达到上限触发的升级。
COST_REASON = "cost_cap_exceeded"

#: 时长预算将尽（软阈值）或已到硬上限触发的升级。
TIME_REASON = "time_budget_exhausted"

#: E2：崩溃/中断时**结果无法确认**的工具副作用（可能已调用、也可能从未调用）。
#: 裁定（ADR §3.2）：默认按"可能已产生副作用"处理——**不重放**，交回用户决定。
UNCERTAIN_SIDE_EFFECTS_REASON = "uncertain_side_effects"

_REASONS = (
    NO_PROGRESS_REASON,
    BUDGET_REASON,
    VERIFICATION_REASON,
    COST_REASON,
    TIME_REASON,
    UNCERTAIN_SIDE_EFFECTS_REASON,
)

#: E2 的引导语：进正文，说明"这次没有重放那些工具"，并把决定交回用户。
UNCERTAIN_SIDE_EFFECTS_GUIDANCE = (
    "这次续跑没有重放上面那些结果无法确认的工具——E 不支持自动重放不确定副作用的工具。"
    "请先确认它们的实际影响（例如那条消息是否已经发出），再决定继续、换个做法，还是人工接管。"
)

OPTION_CONTINUE = "continue"
OPTION_CHANGE_APPROACH = "change_approach"
OPTION_TAKE_OVER = "take_over"

#: 默认预算阈值：用掉 85% 可用上下文就提请人工决策。
DEFAULT_BUDGET_RATIO = 0.85

#: 报告里最多携带的失败尝试条数（避免事件膨胀）。
_MAX_FAILURE_ITEMS = 5


def _require_non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AgentPlatformError(
            "invalid_escalation_input",
            f"{field_name} must be a non-negative integer",
        )
    return value


def budget_ratio(*, used_tokens: int, limit_tokens: Optional[int]) -> Optional[float]:
    """已用/可用比例（无上限或上限为 0 时返回 None）。"""
    _require_non_negative_int(used_tokens, "used_tokens")
    if limit_tokens is None:
        return None
    _require_non_negative_int(limit_tokens, "limit_tokens")
    if limit_tokens == 0:
        return None
    return min(1.0, used_tokens / limit_tokens)


def budget_exhausted(
    *,
    used_tokens: int,
    limit_tokens: Optional[int],
    ratio: float = DEFAULT_BUDGET_RATIO,
) -> bool:
    """上下文用量是否已达到升级阈值（无上限时不误报）。"""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        raise AgentPlatformError(
            "invalid_escalation_input",
            "ratio must be a number",
        )
    if not 0 < float(ratio) <= 1:
        raise AgentPlatformError(
            "invalid_escalation_input",
            "ratio must be within (0, 1]",
        )
    used_ratio = budget_ratio(used_tokens=used_tokens, limit_tokens=limit_tokens)
    if used_ratio is None:
        return False
    return used_ratio >= float(ratio)


@dataclass(frozen=True)
class EscalationProgress:
    """"到目前为止完成了什么"（全部来自运行中的局部计数，无需回查数据库）。"""

    model_turns: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    produced_characters: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: 本次运行已进行的墙钟秒数（时长升级用）。
    elapsed_seconds: float = 0.0
    schema_version: int = ESCALATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.elapsed_seconds, (int, float))
            or isinstance(self.elapsed_seconds, bool)
            or self.elapsed_seconds < 0
        ):
            raise AgentPlatformError(
                "invalid_escalation_input",
                "elapsed_seconds must be a non-negative number",
            )

    def as_json(self) -> dict[str, object]:
        return {
            "modelTurns": self.model_turns,
            "toolCalls": self.tool_calls,
            "toolFailures": self.tool_failures,
            "producedCharacters": self.produced_characters,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "elapsedSeconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class EscalationBudget:
    used_tokens: int
    limit_tokens: Optional[int]
    used_ratio: Optional[float] = None
    #: 已用/上限墙钟秒数（只有时长升级会填）。
    used_seconds: Optional[float] = None
    limit_seconds: Optional[float] = None
    schema_version: int = ESCALATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_non_negative_int(self.used_tokens, "used_tokens")
        if self.limit_tokens is not None:
            _require_non_negative_int(self.limit_tokens, "limit_tokens")
        for name in ("used_seconds", "limit_seconds"):
            value = getattr(self, name)
            if value is None:
                continue
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise AgentPlatformError(
                    "invalid_escalation_input",
                    f"{name} must be a non-negative number or None",
                )
        if self.used_ratio is None:
            object.__setattr__(
                self,
                "used_ratio",
                budget_ratio(
                    used_tokens=self.used_tokens,
                    limit_tokens=self.limit_tokens,
                ),
            )

    def as_json(self) -> dict[str, object]:
        return {
            "usedTokens": self.used_tokens,
            "limitTokens": self.limit_tokens,
            "usedRatio": self.used_ratio,
            "usedSeconds": self.used_seconds,
            "limitSeconds": self.limit_seconds,
        }


@dataclass(frozen=True)
class EscalationReport:
    """一份可直接推给前端的升级报告。"""

    reason: str
    summary: str
    options: tuple[str, ...] = (
        OPTION_CONTINUE,
        OPTION_CHANGE_APPROACH,
        OPTION_TAKE_OVER,
    )
    progress: EscalationProgress = field(default_factory=EscalationProgress)
    budget: EscalationBudget = field(
        default_factory=lambda: EscalationBudget(used_tokens=0, limit_tokens=None)
    )
    repeated_failures: tuple[dict[str, object], ...] = ()
    failures: tuple[dict[str, object], ...] = ()
    guidance: str = ""
    will_stop: bool = False
    #: C1：验证结论（仅 VERIFICATION_REASON 时非空）。
    verdict: Optional[dict[str, object]] = None
    #: C5：成本信息（仅 COST_REASON 时非空）。
    cost: Optional[dict[str, object]] = None
    schema_version: int = ESCALATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.reason not in _REASONS:
            raise AgentPlatformError(
                "invalid_escalation_reason",
                f"unknown escalation reason: {self.reason!r}",
            )

    def as_json(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "summary": self.summary,
            "options": list(self.options),
            "progress": self.progress.as_json(),
            "budget": self.budget.as_json(),
            "repeatedFailures": list(self.repeated_failures),
            "failures": list(self.failures),
            "guidance": self.guidance,
            "willStop": self.will_stop,
            "verdict": self.verdict,
            "cost": self.cost,
        }


def build_escalation_report(
    *,
    reason: str,
    progress: EscalationProgress,
    budget: EscalationBudget,
    memory: Any = None,
    will_stop: bool = False,
    verdict: Any = None,
    cost: Optional[dict[str, object]] = None,
    uncertain_tools: Sequence[Mapping[str, object]] = (),
) -> EscalationReport:
    """按原因生成升级报告（摘要/失败记忆/引导语都在这里定稿）。"""
    if reason not in _REASONS:
        raise AgentPlatformError(
            "invalid_escalation_reason",
            f"unknown escalation reason: {reason!r}",
        )
    repeated = tuple(
        item.as_json() for item in getattr(memory, "repeated", ()) or ()
    )
    failures = tuple(
        getattr(memory, "digest")(limit=_MAX_FAILURE_ITEMS)
        if callable(getattr(memory, "digest", None))
        else ()
    )
    guidance = ""
    if callable(getattr(memory, "guidance", None)):
        guidance = memory.guidance()
    if reason == UNCERTAIN_SIDE_EFFECTS_REASON:
        # E2：引导语由这条通道自己定稿——"没有重放"这件事必须由报告说出来，
        # 不能让它落到无进展那套文案上（那会是一句谎话）。
        guidance = UNCERTAIN_SIDE_EFFECTS_GUIDANCE
    if reason == BUDGET_REASON:
        summary = _budget_summary(budget)
    elif reason == VERIFICATION_REASON:
        summary = _verification_summary(verdict)
    elif reason == COST_REASON:
        # M3-3：同一条成本闸两种语义（父档"只提请" vs 子档"真停止"）。摘要必须
        # 跟着 `will_stop` 走，否则停止那一档会带着"继续推进会继续消耗预算"的字面。
        summary = _cost_summary(cost, will_stop=will_stop)
    elif reason == TIME_REASON:
        summary = _time_summary(budget, progress)
    elif reason == UNCERTAIN_SIDE_EFFECTS_REASON:
        summary = _uncertain_side_effects_summary(uncertain_tools)
    else:
        summary = _no_progress_summary(progress, repeated)
    verdict_json = (
        verdict.as_json() if callable(getattr(verdict, "as_json", None)) else None
    )
    return EscalationReport(
        reason=reason,
        summary=summary,
        progress=progress,
        budget=budget,
        repeated_failures=repeated,
        failures=tuple(failures[0]) if failures else (),
        guidance=guidance,
        will_stop=will_stop,
        verdict=verdict_json,
        cost=cost,
    )


def _budget_summary(budget: EscalationBudget) -> str:
    if budget.used_ratio is None or budget.limit_tokens is None:
        return f"上下文预算将尽（已用 {budget.used_tokens} tokens，未配置上限）。"
    percent = round(budget.used_ratio * 100)
    return (
        f"上下文预算将尽：已用 {budget.used_tokens} / "
        f"{budget.limit_tokens} tokens（{percent}%），继续推进可能需要压缩或换策略。"
    )


def _cost_summary(
    cost: Optional[dict[str, object]],
    *,
    will_stop: bool = False,
) -> str:
    """成本升级的摘要。

    `will_stop=True` 表示这次升级伴随着**真停止**（子运行档的成本上界，M3-3）：
    运行已经在这里收尾，摘要就不能照抄"继续推进会继续消耗预算"——那与同一份报告
    里的 `willStop=true` 自相矛盾。`will_stop=False`（父档只提请）措辞逐字不变。
    """
    if not cost:
        return (
            "本次运行的成本已达到配置上限，已在这里停止。"
            if will_stop
            else "本次运行的成本已达到配置上限。"
        )
    used = cost.get("usedUsd")
    cap = cost.get("capUsd")
    used_text = f"${used:.4f}" if isinstance(used, (int, float)) else "未知"
    cap_text = f"${cap:.4f}" if isinstance(cap, (int, float)) else "未配置"
    if will_stop:
        return (
            f"本次运行成本已达约 {used_text}（上限 {cap_text}），"
            "已按成本上限在这里停止，不会继续消耗预算。"
        )
    return (
        f"本次运行成本已达约 {used_text}（上限 {cap_text}），"
        "继续推进会继续消耗预算。"
    )


def _uncertain_side_effects_summary(
    uncertain_tools: Sequence[Mapping[str, object]],
) -> str:
    """E2 摘要：把"哪些工具、为什么不确定"如实说清（用户据此决定下一步）。

    这里**不能**退化成任何兜底文案（例如"连续无进展"）——那会把
    "结果无法确认"说成另一件事（`escalationReasonLabel` 对未知 reason 也会退化成
    那句话，所以前端标签必须同步改，见 `features/chat/escalation.ts`）。
    """
    names: list[str] = []
    for tool in uncertain_tools:
        name = tool.get("toolName")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    listed = "、".join(names) if names else "未知工具"
    count = len(uncertain_tools)
    return (
        f"上一段执行中断时有 {count} 个工具处于“已发起、结果未知”的状态（{listed}）："
        "它们可能已经产生了副作用（例如消息已经发出去），结果无法确认。"
        "E 不支持自动重放这类工具——请先确认实际影响，再决定下一步。"
    )


def _verification_summary(verdict: Any) -> str:
    reasons = tuple(getattr(verdict, "reasons", ()) or ())
    missing = tuple(getattr(verdict, "missing", ()) or ())
    detail = "；".join(reasons[:3]) if reasons else "验证者未给出具体理由"
    summary = f"独立验证未通过：{detail}。"
    if missing:
        summary += f"缺失：{'、'.join(missing[:3])}。"
    return summary


def _time_summary(
    budget: EscalationBudget,
    progress: EscalationProgress,
) -> str:
    used_seconds = (
        budget.used_seconds
        if budget.used_seconds is not None
        else progress.elapsed_seconds
    )
    minutes = used_seconds / 60
    head = (
        f"本次运行已进行 {minutes:.1f} 分钟"
        f"（模型 {progress.model_turns} 轮、工具调用 {progress.tool_calls} 次）"
    )
    if budget.limit_seconds is None:
        return f"{head}，已到时长阈值。"
    return f"{head}，上限 {budget.limit_seconds / 60:.0f} 分钟，已到时长阈值。"


def _no_progress_summary(
    progress: EscalationProgress,
    repeated: Sequence[dict[str, object]],
) -> str:
    base = (
        f"连续 {progress.model_turns} 轮没有实质进展"
        f"（工具调用 {progress.tool_calls} 次、失败 {progress.tool_failures} 次，"
        f"新增文本 {progress.produced_characters} 字）。"
    )
    if repeated:
        names = "、".join(
            f"{item.get('toolName')}({item.get('errorCode')}×{item.get('count')})"
            for item in repeated[:3]
        )
        return f"{base}反复失败：{names}。"
    return base


#: 硬时长上限收尾时追加进正文的引导语。运行已经结束，所以这里必须告诉用户
#: "怎么继续"——直接发消息会起一个新的运行接着推进。
TIME_BUDGET_GUIDANCE = (
    "【本次运行已到时长上限，已在这里安全收尾】\n"
    "上面是已经完成的阶段性结果。若还需要继续，直接发送消息即可接着推进"
    "（不必重述背景）；也可以换一种思路再继续。"
)

#: 成本上限收尾时追加进正文的引导语（M3-3 子运行档）。与时间到墙是**同一套收尾
#: 语义**（COMPLETED + 引导语），只有"因为什么停下"必须如实写清——成本到顶若照
#: 时长那句写，用户会去改错配置。
COST_BUDGET_GUIDANCE = (
    "【本次运行已到成本上限，已在这里安全收尾】\n"
    "上面是已经完成的阶段性结果。若还需要继续，直接发送消息即可接着推进"
    "（不必重述背景）；也可以换一种思路再继续。"
)


__all__ = [
    "BUDGET_REASON",
    "COST_BUDGET_GUIDANCE",
    "DEFAULT_BUDGET_RATIO",
    "ESCALATION_PROTOCOL_VERSION",
    "EscalationBudget",
    "EscalationProgress",
    "EscalationReport",
    "NO_PROGRESS_REASON",
    "COST_REASON",
    "VERIFICATION_REASON",
    "TIME_BUDGET_GUIDANCE",
    "TIME_REASON",
    "OPTION_CHANGE_APPROACH",
    "OPTION_CONTINUE",
    "OPTION_TAKE_OVER",
    "budget_exhausted",
    "budget_ratio",
    "build_escalation_report",
]
