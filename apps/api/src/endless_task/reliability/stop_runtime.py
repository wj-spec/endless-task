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


__all__ = ["TurnEvidence", "build_turn_signal", "evaluate_turn_history"]
