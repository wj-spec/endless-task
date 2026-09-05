from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from endless_task.runtime_v2.domain import RunRecord, RunStatus


class EvalSeverity(str, Enum):
    """How strongly a metric affects the run verdict."""

    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class EvalUnit(str, Enum):
    NONE = "none"
    BOOL = "bool"
    COUNT = "count"
    MS = "ms"
    TOKENS = "tokens"
    SCORE = "score"


class EvalVerdict(str, Enum):
    """Per-run evaluation conclusion."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class EvalMetric:
    """One scored quantity for a run.

    `value` is intentionally a primitive so it is trivially JSON-serializable and
    diffable. All qualitative nuance lives in `notes`; the machine-readable
    verdict depends only on `severity`.
    """

    key: str
    value: int | float | bool
    unit: EvalUnit
    severity: EvalSeverity
    source: str
    notes: str = ""
    pass_threshold: Optional[int | float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "unit": self.unit.value,
            "severity": self.severity.value,
            "source": self.source,
            "notes": self.notes,
            "pass_threshold": self.pass_threshold,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalMetric":
        return cls(
            key=str(data["key"]),
            value=data["value"],
            unit=EvalUnit(data["unit"]),
            severity=EvalSeverity(data["severity"]),
            source=str(data["source"]),
            notes=str(data.get("notes", "")),
            pass_threshold=data.get("pass_threshold"),
        )


@dataclass(frozen=True)
class ScoreCard:
    """The complete per-run evaluation result."""

    run_id: str
    metrics: tuple[EvalMetric, ...] = ()
    warnings: tuple[str, ...] = ()
    judge_unavailable: bool = False

    @property
    def has_blocker(self) -> bool:
        return any(m.severity is EvalSeverity.BLOCKER for m in self.metrics)

    @property
    def warning_count(self) -> int:
        return sum(1 for m in self.metrics if m.severity is EvalSeverity.WARNING)

    @property
    def verdict(self) -> EvalVerdict:
        if self.judge_unavailable or self.has_blocker:
            return EvalVerdict.FAIL if self.has_blocker else EvalVerdict.WARN
        if self.warning_count > 0:
            return EvalVerdict.WARN
        return EvalVerdict.PASS

    def metric(self, key: str) -> Optional[EvalMetric]:
        for item in self.metrics:
            if item.key == key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "metrics": [m.to_dict() for m in self.metrics],
            "warnings": list(self.warnings),
            "judge_unavailable": self.judge_unavailable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreCard":
        return cls(
            run_id=str(data["run_id"]),
            metrics=tuple(EvalMetric.from_dict(m) for m in data.get("metrics", [])),
            warnings=tuple(data.get("warnings", [])),
            judge_unavailable=bool(data.get("judge_unavailable", False)),
        )


@dataclass(frozen=True)
class RunEvaluation:
    """A single run's stored evaluation, decoupled from the replay machinery."""

    run_id: str
    run_status: RunStatus
    score_card: ScoreCard
    replay_warnings: tuple[str, ...] = ()

    @property
    def verdict(self) -> EvalVerdict:
        return self.score_card.verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_status": self.run_status.value,
            "score_card": self.score_card.to_dict(),
            "replay_warnings": list(self.replay_warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunEvaluation":
        return cls(
            run_id=str(data["run_id"]),
            run_status=RunStatus(data["run_status"]),
            score_card=ScoreCard.from_dict(data["score_card"]),
            replay_warnings=tuple(data.get("replay_warnings", [])),
        )


@dataclass(frozen=True)
class MetricAggregation:
    """Aggregate statistics for one metric across a batch of runs."""

    key: str
    count: int
    mean: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    p95: Optional[float] = None
    pass_rate: Optional[float] = None
    blocker_count: int = 0
    warning_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "count": self.count,
            "mean": self.mean,
            "min": self.min,
            "max": self.max,
            "pass_rate": self.pass_rate,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricAggregation":
        return cls(
            key=str(data["key"]),
            count=int(data.get("count", 0)),
            mean=data.get("mean"),
            min=data.get("min"),
            max=data.get("max"),
            pass_rate=data.get("pass_rate"),
            blocker_count=int(data.get("blocker_count", 0)),
            warning_count=int(data.get("warning_count", 0)),
        )


@dataclass(frozen=True)
class Aggregation:
    """Aggregated batch results plus the pass/fail roll-up."""

    run_count: int
    metrics: dict[str, MetricAggregation]
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0
    judge_unavailable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_count": self.run_count,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "judge_unavailable_count": self.judge_unavailable_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Aggregation":
        return cls(
            run_count=int(data.get("run_count", 0)),
            metrics={
                str(k): MetricAggregation.from_dict(v)
                for k, v in data.get("metrics", {}).items()
            },
            pass_count=int(data.get("pass_count", 0)),
            warn_count=int(data.get("warn_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            judge_unavailable_count=int(data.get("judge_unavailable_count", 0)),
        )


@dataclass(frozen=True)
class MetricDelta:
    key: str
    baseline_value: float
    candidate_value: float
    delta: float
    regressed: bool


@dataclass(frozen=True)
class BaselineDiff:
    """Per-metric comparison of a candidate batch against a baseline batch."""

    baseline_total_pass_rate: float
    candidate_total_pass_rate: float
    deltas: tuple[MetricDelta, ...]
    blocking_regressions: tuple[MetricDelta, ...]

    @property
    def has_blocking_regression(self) -> bool:
        return len(self.blocking_regressions) > 0

    @property
    def exit_code(self) -> int:
        # Non-zero when a blocking regression is detected (CI gate semantics).
        return 1 if self.has_blocking_regression else 0
