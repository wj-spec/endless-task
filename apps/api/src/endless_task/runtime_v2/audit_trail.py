"""A8 决策解释 / 审计轨迹：把运行事实翻译成"为什么"。

《Agentic AI Guide》第 27 章「无障碍与信任 — 解释 Agent 的决策」要求解释四件事：
决策理由、来源归属、反事实、不确定性。本模块做其中**可被事实验证的部分**：

* 理由来自运行期已经存在的事实（工具效果、审批决定、风险等级、错误码、
  升级原因、验证结论、撤销动作），**不是让模型复述自己的思维链**——那样既不
  可靠也不可验证；
* 反事实用"如果当时不做/不批准会怎样"表达，同样基于事实；
* 不确定性只在实际有信号时给出（例如独立验证结论为 uncertain）。

输入是 :class:`AuditFact`（由 API 层从运行事件/工具执行/审批/撤销日志归一化），
输出是排好序的 :class:`AuditEntry`。纯计算、无 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: 有副作用的工具效果。
_WRITE_EFFECTS = ("local_write", "external_action")

_KIND_TITLES = {
    "run": "运行",
    "tool": "工具执行",
    "approval": "审批",
    "effect": "副作用",
    "plan": "计划",
    "memory": "记忆",
    "verification": "独立验证",
    "escalation": "升级人工",
    "undo": "撤销",
}


@dataclass(frozen=True)
class AuditFact:
    """一条运行事实（API 层归一化后的输入）。"""

    fact_id: str
    kind: str
    occurred_at: str
    title: str
    summary: str = ""
    tool_name: str = ""
    effect: str = ""
    risk: str = ""
    decision: str = ""
    error_code: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditEntry:
    """轨迹里的一条（带理由/反事实/不确定性）。"""

    fact_id: str
    kind: str
    title: str
    summary: str
    rationale: str
    counterfactual: str
    uncertainty: str
    severity: str
    occurred_at: str
    tool_name: str = ""
    effect: str = ""
    risk: str = ""
    decision: str = ""
    options: tuple[str, ...] = ()
    refs: Mapping[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.fact_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "rationale": self.rationale,
            "counterfactual": self.counterfactual,
            "uncertainty": self.uncertainty,
            "severity": self.severity,
            "occurredAt": self.occurred_at,
            "toolName": self.tool_name,
            "effect": self.effect,
            "risk": self.risk,
            "decision": self.decision,
            "options": list(self.options),
            "refs": dict(self.refs),
        }


def severity_for(fact: AuditFact) -> str:
    """严重度只反映"这件事值不值得你回头看"。"""
    if fact.kind == "approval" and fact.decision == "deny":
        return SEVERITY_CRITICAL
    if fact.kind == "verification" and str(
        fact.payload.get("verdict") or ""
    ) == "fail":
        return SEVERITY_CRITICAL
    if fact.error_code:
        return SEVERITY_CRITICAL
    if fact.effect in _WRITE_EFFECTS or fact.kind in ("escalation", "undo"):
        return SEVERITY_WARNING
    if fact.kind == "verification":
        return SEVERITY_WARNING
    return SEVERITY_INFO


def counterfactual_for(fact: AuditFact) -> str:
    """反事实：如果不这么做/不这样决定，会怎样（只对有影响的事给）。"""
    if fact.kind == "approval" and fact.decision == "deny":
        return (
            f"如果你当时批准，它会继续执行「{fact.tool_name or '该工具'}」；"
            "现在它没有执行。"
        )
    if fact.kind == "approval" and fact.decision == "modify":
        return "如果你当时直接批准原参数，它会按原来的参数执行。"
    if fact.kind == "undo":
        target = str(fact.payload.get("target") or fact.summary or "该文件")
        return f"如果不撤销，{target} 会保持这次改动后的内容。"
    if fact.kind == "tool" and fact.effect in _WRITE_EFFECTS:
        return "如果不批准/不执行这一步，目标文件不会被改动；如需回退可用「撤销」。"
    if fact.kind == "escalation":
        return "如果继续自动推进，可能会重复已经失败的方法。"
    if fact.kind == "verification" and str(
        fact.payload.get("verdict") or ""
    ) == "fail":
        return "如果没有这道独立验证，这个结果会被直接当成完成。"
    return ""


def _rationale_for(fact: AuditFact) -> str:
    if fact.kind == "approval":
        risk = f"风险等级 {fact.risk}" if fact.risk else "风险等级未标注"
        if fact.decision == "deny":
            return f"你拒绝了这次操作（{risk}）；运行时因此没有执行它。"
        if fact.decision == "modify":
            return f"你修改了参数后再批准（{risk}），执行用的是你改过的参数。"
        if fact.decision == "approve":
            return f"你批准了这次操作（{risk}），运行时随后执行了它。"
        return f"这是一次待你确认的操作（{risk}）。"
    if fact.kind == "tool":
        if fact.error_code:
            return (
                f"工具「{fact.tool_name or '未知'}」执行失败（{fact.error_code}）；"
                "失败原因会进入后续决策，避免原样重试。"
            )
        if fact.effect == "read_only":
            return "这是只读操作，不会改变任何数据，因此自动执行。"
        if fact.effect in _WRITE_EFFECTS:
            label = fact.title or fact.tool_name or "该动作"
            return (
                f"这一步会改动数据（{fact.effect}）：{label}；"
                "运行时把它作为需要留痕的动作。"
            )
        return "这一步没有副作用。"
    if fact.kind == "escalation":
        summary = str(fact.payload.get("summary") or fact.summary or "")
        reason = str(fact.payload.get("reason") or "")
        label = {
            "no_progress": "连续没有实质进展",
            "budget_exhausted": "上下文预算将尽",
            "cost_cap_exceeded": "成本达到上限",
            "verification_failed": "独立验证未通过",
        }.get(reason, reason or "需要人工判断")
        return f"触发升级：{label}。{summary}".strip()
    if fact.kind == "verification":
        verdict = str(fact.payload.get("verdict") or "")
        reasons = "；".join(
            str(item) for item in (fact.payload.get("reasons") or ())
        )
        missing = "、".join(
            str(item) for item in (fact.payload.get("missing") or ())
        )
        label = {
            "pass": "通过",
            "fail": "未通过",
            "uncertain": "无法判定",
        }.get(verdict, "未知结论")
        text = f"独立验证（不看制造者的推理）判定：{label}。"
        if reasons:
            text += f"理由：{reasons}。"
        if missing:
            text += f"缺失：{missing}。"
        return text
    if fact.kind == "undo":
        return f"你回滚了这一步改动（{fact.summary or fact.title}）。"
    if fact.kind == "memory":
        return "这一步写入/合并了长期记忆，因此需要留痕以便溯源。"
    if fact.kind == "plan":
        return "这一步更新了执行计划，后续步骤会按新计划推进。"
    if fact.kind == "run":
        return fact.summary or "运行状态变化。"
    return fact.summary or "记录了这一步。"


def _uncertainty_for(fact: AuditFact) -> str:
    if fact.kind != "verification":
        return ""
    verdict = str(fact.payload.get("verdict") or "")
    if verdict == "uncertain":
        return "验证器本身不确定（信息不足或输出无法解析），不能当作通过。"
    if verdict == "pass":
        return "验证通过不等于绝对正确，仍建议按需复核。"
    if verdict == "fail":
        return "验证器也可能误判；重要改动建议人工复核后再采用。"
    return ""


def build_audit_trail(facts: Sequence[AuditFact]) -> tuple[AuditEntry, ...]:
    """把事实按时间排成一条可审阅的轨迹。"""
    entries: list[AuditEntry] = []
    for fact in sorted(facts, key=lambda item: (item.occurred_at, item.fact_id)):
        options = tuple(
            str(item) for item in (fact.payload.get("options") or ())
        )
        refs = {
            str(key): str(value)
            for key, value in (fact.payload.get("refs") or {}).items()
            if value
        }
        entries.append(
            AuditEntry(
                fact_id=fact.fact_id,
                kind=fact.kind,
                title=fact.title or _KIND_TITLES.get(fact.kind, fact.kind),
                summary=fact.summary,
                rationale=_rationale_for(fact),
                counterfactual=counterfactual_for(fact),
                uncertainty=_uncertainty_for(fact),
                severity=severity_for(fact),
                occurred_at=fact.occurred_at,
                tool_name=fact.tool_name,
                effect=fact.effect,
                risk=fact.risk,
                decision=fact.decision,
                options=options,
                refs=refs,
            )
        )
    return tuple(entries)


__all__ = [
    "AuditEntry",
    "AuditFact",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "build_audit_trail",
    "counterfactual_for",
    "severity_for",
]
