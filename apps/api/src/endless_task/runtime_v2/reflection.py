"""B4 反思（Reflection）：从失败中提炼可复用的洞见。

《Agentic AI Guide》第 27 章「智能体记忆系统 — 反思：元认知操作」：

    Reflect(M) → insights    输入=情景记忆，输出=语义记忆（归纳后的洞见）

Reflexion 的经典例子是"三次失败后反思 → 我总是忘记处理空输入 → 下次显式检查"。
本模块是其中的**纯计算**部分：把运行里的"情景证据"（连续同因失败、升级原因、
运行失败）按固定规则归纳成一条**可复用、可验证**的洞见文本。

刻意**不依赖模型**：

* 洞见只在证据充分时产生（同一工具同一错误连续 ≥2 次、或明确的升级原因）；
* 文本是规则生成的，因此可解释、可测试、不会产生"看起来很聪明但没依据"的噪音；
* 洞见本身仍走记忆提案确认（服务层），用户不满意可以拒绝。

模型生成的反思留作后续（需要额外一轮推理，且必须标注"参考"）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 洞见按"重要记忆"对待（B3：重要性 ≥0.7 不会被自动遗忘）。
DEFAULT_INSIGHT_IMPORTANCE = 0.8

#: 连续同因失败达到该次数才值得反思。
REFLECTION_FAILURE_THRESHOLD = 2

_REASON_LABELS = {
    "no_progress": "连续无进展",
    "verification_failed": "独立验证未通过",
    "budget_exhausted": "上下文预算将尽",
    "cost_cap_exceeded": "成本达到上限",
}


@dataclass(frozen=True)
class ReflectionEpisode:
    """一条情景证据（来自运行记录/事件）。"""

    kind: str
    tool_name: str = ""
    error_code: str = ""
    count: int = 1
    reason: str = ""
    refs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReflectionInsight:
    """一条归纳出的语义洞见。"""

    trigger: str
    content: str
    reason: str
    signature: str
    refs: tuple[dict[str, Any], ...] = ()
    importance: float = DEFAULT_INSIGHT_IMPORTANCE

    def as_json(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "content": self.content,
            "reason": self.reason,
            "signature": self.signature,
            "refs": [dict(item) for item in self.refs],
            "importance": self.importance,
        }


def should_reflect(
    *,
    run_status: str,
    escalation_reasons: Sequence[str] = (),
    failure_streak: int = 0,
) -> bool:
    """是否值得为这次运行做反思（宁缺勿滥）。"""
    if str(run_status) == "failed":
        return True
    if any(reason for reason in escalation_reasons):
        return True
    return int(failure_streak) >= REFLECTION_FAILURE_THRESHOLD


def insight_signature(episodes: Iterable[ReflectionEpisode]) -> str:
    """情景证据集合的稳定签名（顺序无关，用于"同一教训不重复提"）。"""
    parts = sorted(
        f"{episode.kind}|{episode.tool_name}|{episode.error_code}|"
        f"{episode.reason}|{episode.count}"
        for episode in episodes
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def derive_insights(
    episodes: Iterable[ReflectionEpisode],
) -> tuple[ReflectionInsight, ...]:
    """按规则把情景证据归纳成洞见（同一教训只出一条）。"""
    episode_list = list(episodes)
    insights: list[ReflectionInsight] = []
    seen: set[str] = set()

    # 1) 连续同因失败：按 (工具, 错误码) 聚合。
    failures: dict[tuple[str, str], list[ReflectionEpisode]] = {}
    for episode in episode_list:
        if episode.kind != "tool_failure":
            continue
        key = (episode.tool_name, episode.error_code)
        failures.setdefault(key, []).append(episode)
    for (tool_name, error_code), items in sorted(failures.items()):
        total = sum(max(0, int(item.count)) for item in items)
        if total < REFLECTION_FAILURE_THRESHOLD:
            continue
        content = (
            f"工具 {tool_name or '某工具'} 曾因 {error_code or '未知错误'} "
            f"连续失败 {total} 次：下次调用前先确认该错误的前置条件，"
            "失败后不要原样重试，先换参数或换方法。"
        )
        signature = insight_signature(items)
        if signature in seen:
            continue
        seen.add(signature)
        insights.append(
            ReflectionInsight(
                trigger="tool_failure",
                content=content,
                reason=(
                    f"反思：{tool_name or '工具'} 连续失败 {total} 次"
                    f"（{error_code or '未知错误'}）"
                ),
                signature=signature,
                refs=tuple(dict(item.refs) for item in items),
            )
        )

    # 2) 升级原因：每种原因一条固定教训。
    by_reason: dict[str, list[ReflectionEpisode]] = {}
    for episode in episode_list:
        if episode.kind != "escalation" or not episode.reason:
            continue
        by_reason.setdefault(episode.reason, []).append(episode)
    for reason, items in sorted(by_reason.items()):
        content = _escalation_lesson(reason, items)
        if content is None:
            continue
        signature = insight_signature(items)
        if signature in seen:
            continue
        seen.add(signature)
        label = _REASON_LABELS.get(reason, reason)
        insights.append(
            ReflectionInsight(
                trigger=f"escalation:{reason}",
                content=content,
                reason=f"反思：本次运行出现「{label}」",
                signature=signature,
                refs=tuple(dict(item.refs) for item in items),
            )
        )

    # 3) 运行失败：把错误码变成"下次先读错误再动手"。
    run_failures = [
        episode for episode in episode_list if episode.kind == "run_failure"
    ]
    if run_failures:
        codes = sorted(
            {episode.error_code for episode in run_failures if episode.error_code}
        )
        content = (
            f"这次运行失败了（{codes[0]}）：下次遇到同类失败先读错误信息"
            "再决定下一步，不要重复同一次调用。"
            if codes
            else "这次运行失败了：下次遇到同类失败先读错误信息再决定下一步，"
            "不要重复同一次调用。"
        )
        signature = insight_signature(run_failures)
        if signature not in seen:
            seen.add(signature)
            insights.append(
                ReflectionInsight(
                    trigger="run_failure",
                    content=content,
                    reason="反思：本次运行失败",
                    signature=signature,
                    refs=tuple(dict(item.refs) for item in run_failures),
                )
            )
    return tuple(insights)


def _escalation_lesson(
    reason: str,
    items: Sequence[ReflectionEpisode],
) -> str | None:
    if reason == "no_progress":
        return (
            "遇到连续无进展时：先把任务拆成更小的步骤或换一种方法，"
            "而不是重复同一次调用。"
        )
    if reason == "verification_failed":
        detail = ""
        for item in items:
            summary = str(item.refs.get("summary") or "").strip()
            if summary:
                detail = f"（{summary}）"
                break
        return f"产出前先自检目标是否真的达成{detail}，再宣布完成。"
    if reason in ("budget_exhausted", "cost_cap_exceeded"):
        return (
            "接近预算上限时：先压缩上下文或缩小任务范围，"
            "不要继续在无关探索上消耗预算。"
        )
    return None


__all__ = [
    "DEFAULT_INSIGHT_IMPORTANCE",
    "REFLECTION_FAILURE_THRESHOLD",
    "ReflectionEpisode",
    "ReflectionInsight",
    "derive_insights",
    "insight_signature",
    "should_reflect",
]
