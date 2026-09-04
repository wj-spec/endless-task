"""Runtime adapter for no-progress shadow evaluation (M2 prelude Stage 2).

Bridges per-turn runtime evidence (context fingerprint + tool executions)
into :class:`ProgressSignal` values and evaluates them with the stop policy
profile. Pure and deterministic; the caller (AgentRunExecutor) feeds turn
evidence in order and receives the evaluation — observation only, no loop
semantics are changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

from .stop import (
    NoProgressEvaluation,
    ProgressSignal,
    StopPolicyProfile,
    evaluate_no_progress,
)


@dataclass(frozen=True)
class TurnEvidence:
    context_fingerprint: str
    tool_signature: Optional[str] = None
    tool_outcome_fingerprint: Optional[str] = None
    unresolved_error: Optional[str] = None
    artifact_changes: int = 0
    checkpoint_changes: int = 0


def build_turn_signal(evidence: TurnEvidence) -> ProgressSignal:
    if not isinstance(evidence, TurnEvidence):
        raise AgentPlatformError(
            "invalid_turn_evidence",
            "Signal building requires TurnEvidence",
        )
    return ProgressSignal(
        context_fingerprint=evidence.context_fingerprint,
        tool_signature=evidence.tool_signature,
        tool_outcome_fingerprint=evidence.tool_outcome_fingerprint,
        artifact_changes=evidence.artifact_changes,
        checkpoint_changes=evidence.checkpoint_changes,
        unresolved_error=evidence.unresolved_error,
    )


def evaluate_turn_history(
    evidences: Sequence[TurnEvidence],
    profile: StopPolicyProfile,
) -> NoProgressEvaluation:
    if isinstance(evidences, (str, bytes)):
        raise AgentPlatformError(
            "invalid_turn_evidence",
            "evidences must be a sequence of TurnEvidence values",
        )
    evidence_tuple = tuple(evidences)
    if any(not isinstance(item, TurnEvidence) for item in evidence_tuple):
        raise AgentPlatformError(
            "invalid_turn_evidence",
            "evidences must be TurnEvidence values",
        )
    signals = tuple(build_turn_signal(item) for item in evidence_tuple)
    return evaluate_no_progress(signals, profile)


def no_progress_guidance(level, reasons: Sequence[str]) -> str:
    """Structured model-facing guidance for REMIND/RESTRICT/STOP levels."""
    from .stop import StopLevel

    if level is StopLevel.STOP:
        return (
            "系统已安全停止本轮：连续多轮未产生实质进展。"
            "请向用户说明当前结论与卡点，不要继续重复尝试。"
        )
    if level is StopLevel.RESTRICT:
        return (
            "系统检测到连续多轮无实质进展（重复工具/结果或连续失败）。"
            "请改变策略：先向用户确认方向，或改用不同的工具与输入；"
            "不要原样重复同一调用。"
        )
    return (
        "系统提示：最近几轮未见实质进展。请重新审视目标，"
        "必要时先说明计划或向用户询问，而不是继续重复尝试。"
    )


__all__ = [
    "TurnEvidence",
    "build_turn_signal",
    "evaluate_turn_history",
    "no_progress_guidance",
]
