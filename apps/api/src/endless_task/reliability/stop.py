"""Progress-aware stop policy (M3A, doc 05 §4.3).

The old "fixed turn-count" stop is deliberately not used (05 design): the
stop policy detects "state is not changing" from a history of
:class:`ProgressSignal` values and escalates in three levels:

1. ``REMIND``  — inject a structured no-progress reminder,
2. ``RESTRICT`` — limit repeated tools / demand a strategy change,
3. ``STOP``    — safe stop, preserving recoverable state.

Thresholds come from a profile and are never hard-coded in the loop. This
module is deterministic and clock-free: it evaluates consecutive signals
only; wall-clock deadlines and time-based convergence belong to the engine
wiring (RS-2 acceptance evaluation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
)

STOP_PROTOCOL_VERSION = 1


class StopLevel(str, Enum):
    NONE = "none"
    REMIND = "remind"
    RESTRICT = "restrict"
    STOP = "stop"


@dataclass(frozen=True)
class ProgressSignal:
    context_fingerprint: str
    tool_signature: Optional[str] = None
    tool_outcome_fingerprint: Optional[str] = None
    artifact_changes: int = 0
    checkpoint_changes: int = 0
    unresolved_error: Optional[str] = None
    schema_version: int = STOP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=STOP_PROTOCOL_VERSION,
            protocol="progress_signal",
        )
        object.__setattr__(
            self,
            "context_fingerprint",
            require_identifier(
                self.context_fingerprint,
                field_name="context_fingerprint",
                max_length=64,
            ),
        )
        if self.tool_signature is not None:
            object.__setattr__(
                self,
                "tool_signature",
                require_identifier(
                    self.tool_signature,
                    field_name="tool_signature",
                    max_length=256,
                ),
            )
        if self.tool_outcome_fingerprint is not None:
            object.__setattr__(
                self,
                "tool_outcome_fingerprint",
                require_identifier(
                    self.tool_outcome_fingerprint,
                    field_name="tool_outcome_fingerprint",
                    max_length=64,
                ),
            )
        for field_name in ("artifact_changes", "checkpoint_changes"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AgentPlatformError(
                    "invalid_progress_signal",
                    f"{field_name} must be a non-negative integer",
                )
        if self.unresolved_error is not None:
            object.__setattr__(
                self,
                "unresolved_error",
                require_identifier(
                    self.unresolved_error,
                    field_name="unresolved_error",
                    max_length=128,
                ),
            )


@dataclass(frozen=True)
class StopPolicyProfile:
    """Escalation thresholds (consecutive no-progress signals)."""

    remind_after: int = 1
    restrict_after: int = 2
    stop_after: int = 3
    schema_version: int = STOP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=STOP_PROTOCOL_VERSION,
            protocol="stop_policy_profile",
        )
        values = (
            self.remind_after,
            self.restrict_after,
            self.stop_after,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in values
        ):
            raise AgentPlatformError(
                "invalid_stop_policy_profile",
                "Stop policy thresholds must be positive integers",
            )
        if not self.remind_after < self.restrict_after < self.stop_after:
            raise AgentPlatformError(
                "invalid_stop_policy_profile",
                "Stop thresholds must satisfy remind < restrict < stop",
            )


@dataclass(frozen=True)
class NoProgressEvaluation:
    level: StopLevel
    consecutive: int
    detector: str
    reasons: tuple[str, ...]
    schema_version: int = STOP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=STOP_PROTOCOL_VERSION,
            protocol="no_progress_evaluation",
        )
        if not isinstance(self.level, StopLevel):
            raise AgentPlatformError(
                "invalid_no_progress_evaluation",
                "Evaluation level must be a protocol enum value",
            )
        if not isinstance(self.consecutive, int) or self.consecutive < 0:
            raise AgentPlatformError(
                "invalid_no_progress_evaluation",
                "Consecutive count must be non-negative",
            )


def evaluate_no_progress(
    signals: Sequence[ProgressSignal],
    profile: StopPolicyProfile,
) -> NoProgressEvaluation:
    """Evaluate the trailing run of no-progress signals."""
    if isinstance(signals, (str, bytes)):
        raise AgentPlatformError(
            "invalid_progress_evaluation",
            "signals must be a sequence of ProgressSignal values",
        )
    signal_tuple = tuple(signals)
    if any(not isinstance(signal, ProgressSignal) for signal in signal_tuple):
        raise AgentPlatformError(
            "invalid_progress_evaluation",
            "signals must be ProgressSignal values",
        )
    if not isinstance(profile, StopPolicyProfile):
        raise AgentPlatformError(
            "invalid_progress_evaluation",
            "No-progress evaluation requires a StopPolicyProfile",
        )
    if not signal_tuple:
        return NoProgressEvaluation(
            level=StopLevel.NONE,
            consecutive=0,
            detector="empty_history",
            reasons=(),
        )
    best_run = _longest_no_progress_run(signal_tuple)
    # A single differing signal is not yet a "repeat": no-progress runs need
    # at least two consecutive unchanged states.
    consecutive = len(best_run) if len(best_run) >= 2 else 0
    if consecutive == 0:
        return NoProgressEvaluation(
            level=StopLevel.NONE,
            consecutive=0,
            detector="progress_detected",
            reasons=(),
        )
    if consecutive >= profile.stop_after:
        level = StopLevel.STOP
    elif consecutive >= profile.restrict_after:
        level = StopLevel.RESTRICT
    elif consecutive >= profile.remind_after:
        level = StopLevel.REMIND
    else:  # pragma: no cover - run length >= 1 implies >= remind
        level = StopLevel.NONE
    return NoProgressEvaluation(
        level=level,
        consecutive=consecutive,
        detector=_detector(best_run),
        reasons=_reasons(best_run),
    )


def _longest_no_progress_run(
    signals: tuple[ProgressSignal, ...],
) -> tuple[ProgressSignal, ...]:
    """Longest contiguous run where every adjacent pair shows no change."""
    best_start = 0
    best_length = 1
    run_start = 0
    for index in range(len(signals) - 1):
        if _no_change(signals[index], signals[index + 1]):
            length = index + 2 - run_start
            if length > best_length:
                best_start = run_start
                best_length = length
        else:
            run_start = index + 1
    return tuple(signals[best_start : best_start + best_length])


def _no_change(previous: ProgressSignal, current: ProgressSignal) -> bool:
    """Whether moving from ``previous`` to ``current`` shows no progress.

    Doc 05 distinguishes the two repeated-call detectors: an identical tool
    signature + outcome fingerprint counts regardless of context growth
    (repeated identical calls), while repeated failures only count when the
    context fingerprint is unchanged.
    """
    if current.artifact_changes > 0 or current.checkpoint_changes > 0:
        return False
    if current.unresolved_error is not None:
        return (
            previous.unresolved_error is not None
            and previous.context_fingerprint == current.context_fingerprint
        )
    if current.tool_signature is not None:
        return (
            previous.tool_signature == current.tool_signature
            and previous.tool_outcome_fingerprint == current.tool_outcome_fingerprint
        )
    # Empty turn without error: no change only if the previous turn was also
    # empty (no tool, no error) with an unchanged context.
    return (
        previous.context_fingerprint == current.context_fingerprint
        and previous.tool_signature is None
        and previous.unresolved_error is None
        and previous.artifact_changes == 0
        and previous.checkpoint_changes == 0
    )


def _detector(signals: tuple[ProgressSignal, ...]) -> str:
    last = signals[-1]
    if last.unresolved_error is not None:
        return "repeated_failure"
    if last.tool_signature is not None:
        return "identical_tool_outcome"
    return "no_observable_change"


def _reasons(signals: tuple[ProgressSignal, ...]) -> tuple[str, ...]:
    reasons: list[str] = []
    last = signals[-1]
    if last.unresolved_error is not None:
        reasons.append("连续失败且上下文指纹未变化")
    if last.tool_signature is not None and last.tool_outcome_fingerprint is not None:
        reasons.append("相同工具与相同结果指纹连续出现")
    if (
        last.tool_signature is None
        and last.artifact_changes == 0
        and last.checkpoint_changes == 0
        and last.unresolved_error is None
    ):
        reasons.append("多轮无新增文本/Artifact/Memory/Effect/checkpoint")
    return tuple(reasons)


__all__ = [
    "NoProgressEvaluation",
    "ProgressSignal",
    "STOP_PROTOCOL_VERSION",
    "StopLevel",
    "StopPolicyProfile",
    "evaluate_no_progress",
]
