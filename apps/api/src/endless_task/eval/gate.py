"""Release gate over Eval suites vs a frozen baseline (M6 OE-4).

08 §10.3: regression thresholds are frozen by **baseline data**, not
hard-coded in docs; a quality-gate regression requires an explicit
waiver recording reason, expiry and owner. This module turns that rule
into code:

- :class:`EvalBaseline` freezes one suite's aggregation (the reference
  evidence a release checklist cites),
- :func:`run_gate` diffs a candidate batch against the baseline and
  reports blocking regressions,
- :class:`Waiver` explicitly releases a specific key for a reason, until
  an expiry date, owned by someone; any *other* blocking regression still
  fails the gate,
- reports are produced in machine-readable JSON and human-readable
  markdown for CI.

The gate consumes :class:`Aggregation` objects, so a suite batch can come
from repo-run evaluations or from trajectory bundles (OE-4 trajectory
connection) alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from io import StringIO
from typing import Any, Mapping, Optional, Sequence, Tuple

from .aggregator import DEFAULT_BLOCKING_KEYS, diff
from .models import Aggregation, MetricDelta


@dataclass(frozen=True)
class EvalBaseline:
    """A frozen reference aggregation for one suite."""

    suite_name: str
    revision: str
    aggregation: Aggregation
    tolerances: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "suite": self.suite_name,
            "revision": self.revision,
            "aggregation": self.aggregation.to_dict(),
            "tolerances": dict(self.tolerances),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvalBaseline":
        return cls(
            suite_name=str(data["suite"]),
            revision=str(data["revision"]),
            aggregation=Aggregation.from_dict(data["aggregation"]),
            tolerances={
                str(k): float(v) for k, v in data.get("tolerances", {}).items()
            },
        )


@dataclass(frozen=True)
class Waiver:
    """Explicit permission for one metric key to regress past the gate."""

    metric_key: str
    reason: str
    owner: str
    expires_at: date

    def is_active(self, *, on: Optional[date] = None) -> bool:
        return (on or date.today()) <= self.expires_at


@dataclass(frozen=True)
class GateResult:
    """One suite gate run: blocking regressions vs. waived keys."""

    suite_name: str
    baseline_revision: str
    baseline_pass_rate: float
    candidate_pass_rate: float
    regressions: Tuple[MetricDelta, ...] = ()
    blocked: Tuple[MetricDelta, ...] = ()
    waived: Tuple[str, ...] = ()
    expired_waivers: Tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return len(self.blocked) == 0

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "suite": self.suite_name,
            "baselineRevision": self.baseline_revision,
            "baselinePassRate": self.baseline_pass_rate,
            "candidatePassRate": self.candidate_pass_rate,
            "regressions": [
                {
                    "key": item.key,
                    "baseline": item.baseline_value,
                    "candidate": item.candidate_value,
                    "delta": item.delta,
                }
                for item in self.regressions
            ],
            "blocked": [item.key for item in self.blocked],
            "waived": list(self.waived),
            "expiredWaivers": list(self.expired_waivers),
            "passed": self.passed,
        }


def run_gate(
    *,
    suite_name: str,
    baseline: EvalBaseline,
    candidate: Aggregation,
    waivers: Sequence[Waiver] = (),
    blocking_keys: Sequence[str] = (),
    on: Optional[date] = None,
) -> GateResult:
    """Compare a candidate suite batch against its frozen baseline.

    Blocking keys default to the aggregation-level defaults; a blocking
    regression is released only by an active waiver naming the same key.
    """
    if not blocking_keys:
        blocking_keys = DEFAULT_BLOCKING_KEYS
    comparison = diff(
        baseline.aggregation,
        candidate,
        tolerances=baseline.tolerances,
        blocking_keys=blocking_keys,
    )
    waived_keys = {
        waiver.metric_key
        for waiver in waivers
        if waiver.is_active(on=on)
    }
    expired_waiver_keys = {
        waiver.metric_key
        for waiver in waivers
        if not waiver.is_active(on=on)
    }
    blocked: list[MetricDelta] = []
    waived_names: list[str] = []
    for item in comparison.blocking_regressions:
        if item.key in waived_keys:
            waived_names.append(item.key)
        else:
            blocked.append(item)
    return GateResult(
        suite_name=suite_name,
        baseline_revision=baseline.revision,
        baseline_pass_rate=comparison.baseline_total_pass_rate,
        candidate_pass_rate=comparison.candidate_total_pass_rate,
        regressions=comparison.deltas,
        blocked=tuple(blocked),
        waived=tuple(sorted(waived_names)),
        expired_waivers=tuple(sorted(expired_waiver_keys)),
    )


def gate_to_json(result: GateResult) -> str:
    """Machine-readable gate report for CI."""
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def gate_to_markdown(result: GateResult) -> str:
    """Human-readable gate report."""
    buffer = StringIO()
    buffer.write(
        f"## Eval gate: {result.suite_name} "
        f"(baseline {result.baseline_revision})\n\n"
    )
    buffer.write(
        f"pass_rate {result.candidate_pass_rate:.4f} vs baseline "
        f"{result.baseline_pass_rate:.4f}\n\n"
    )
    if not result.regressions:
        buffer.write("no metric regressions\n\n")
        return buffer.getvalue()
    buffer.write("| metric | baseline | candidate | delta |\n")
    buffer.write("|---|---|---|---|\n")
    for item in result.regressions:
        buffer.write(
            f"| {item.key} | {item.baseline_value:.4f} | "
            f"{item.candidate_value:.4f} | {item.delta:+.4f} |\n"
        )
    buffer.write("\n")
    if result.waived:
        buffer.write(f"waived: {', '.join(result.waived)}\n")
    if result.expired_waivers:
        buffer.write(f"expired waivers: {', '.join(result.expired_waivers)}\n")
    if result.blocked:
        buffer.write(
            "blocking regressions: " + ", ".join(item.key for item in result.blocked)
            + "\n"
        )
    elif result.regressions:
        buffer.write("no blocking regressions\n")
    return buffer.getvalue()


__all__ = [
    "EvalBaseline",
    "GateResult",
    "Waiver",
    "gate_to_json",
    "gate_to_markdown",
    "run_gate",
]
