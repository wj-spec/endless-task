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

_REASONS = (NO_PROGRESS_REASON, BUDGET_REASON)

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
    schema_version: int = ESCALATION_PROTOCOL_VERSION

    def as_json(self) -> dict[str, object]:
        return {
            "modelTurns": self.model_turns,
            "toolCalls": self.tool_calls,
            "toolFailures": self.tool_failures,
            "producedCharacters": self.produced_characters,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
        }


@dataclass(frozen=True)
class EscalationBudget:
    used_tokens: int
    limit_tokens: Optional[int]
    used_ratio: Optional[float] = None
    schema_version: int = ESCALATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_non_negative_int(self.used_tokens, "used_tokens")
        if self.limit_tokens is not None:
            _require_non_negative_int(self.limit_tokens, "limit_tokens")
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
        }


def build_escalation_report(
    *,
    reason: str,
    progress: EscalationProgress,
    budget: EscalationBudget,
    memory: Any = None,
    will_stop: bool = False,
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
    if reason == BUDGET_REASON:
        summary = _budget_summary(budget)
    else:
        summary = _no_progress_summary(progress, repeated)
    return EscalationReport(
        reason=reason,
        summary=summary,
        progress=progress,
        budget=budget,
        repeated_failures=repeated,
        failures=tuple(failures[0]) if failures else (),
        guidance=guidance,
        will_stop=will_stop,
    )


def _budget_summary(budget: EscalationBudget) -> str:
    if budget.used_ratio is None or budget.limit_tokens is None:
        return f"上下文预算将尽（已用 {budget.used_tokens} tokens，未配置上限）。"
    percent = round(budget.used_ratio * 100)
    return (
        f"上下文预算将尽：已用 {budget.used_tokens} / "
        f"{budget.limit_tokens} tokens（{percent}%），继续推进可能需要压缩或换策略。"
    )


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


__all__ = [
    "BUDGET_REASON",
    "DEFAULT_BUDGET_RATIO",
    "ESCALATION_PROTOCOL_VERSION",
    "EscalationBudget",
    "EscalationProgress",
    "EscalationReport",
    "NO_PROGRESS_REASON",
    "OPTION_CHANGE_APPROACH",
    "OPTION_CONTINUE",
    "OPTION_TAKE_OVER",
    "budget_exhausted",
    "budget_ratio",
    "build_escalation_report",
]
