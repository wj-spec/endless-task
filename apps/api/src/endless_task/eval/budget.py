"""Approved budget enforcement for eval gates (G1 质量 gap).

08 §10.3: "P95 latency 和 cost 不超过批准预算" — but the gate only
compared candidate vs baseline regressions; there was no mechanism to
fail a candidate that exceeds an **absolute approved budget** (a cost or
latency ceiling, independent of any baseline).

This module adds that mechanism:

- :class:`ApprovedBudget` pins a per-metric absolute ceiling (e.g.
  ``trajectory_cost_usd_total`` <= 0.05 USD per run average),
- :func:`check_budgets` compares a candidate aggregation's per-metric
  mean (the per-run average) against the ceilings and reports violations,
- the CLI/``run_gate`` callers fold violations into the gate result as
  blocking when the metric is not covered by an active waiver.

Budgets are data-driven (like suites): a budget file is a JSON map of
metric key -> ceiling, so release thresholds stay config, not code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .models import Aggregation


@dataclass(frozen=True)
class ApprovedBudget:
    """One approved absolute ceiling for a metric.

    ``stat`` selects which aggregate the ceiling applies to: ``mean``
    (default, back-compatible) or ``p95`` (latency budgets, 07).
    """

    metric_key: str
    ceiling: float
    stat: str = "mean"

    def __post_init__(self) -> None:
        if not isinstance(self.ceiling, (int, float)) or isinstance(
            self.ceiling, bool
        ):
            raise ValueError("budget ceiling must be numeric")
        if self.ceiling < 0:
            raise ValueError("budget ceiling must be non-negative")
        if self.stat not in ("mean", "p95"):
            raise ValueError("budget stat must be 'mean' or 'p95'")


@dataclass(frozen=True)
class BudgetViolation:
    metric_key: str
    ceiling: float
    actual: float
    stat: str = "mean"

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "metric": self.metric_key,
            "ceiling": self.ceiling,
            "actual": self.actual,
            "stat": self.stat,
        }


@dataclass(frozen=True)
class BudgetResult:
    """Budget check outcome for one aggregation."""

    violations: tuple[BudgetViolation, ...] = ()

    @property
    def passed(self) -> bool:
        return len(self.violations) == 0

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "violations": [item.to_dict() for item in self.violations],
        }


def load_budgets(path: Path) -> tuple[ApprovedBudget, ...]:
    """Load a budget file: JSON map of metric key -> ceiling."""
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("budget file must be a JSON object of metric -> ceiling")
    budgets = []
    for key, raw in parsed.items():
        if isinstance(raw, dict):
            budgets.append(
                ApprovedBudget(
                    metric_key=str(key),
                    ceiling=float(raw.get("ceiling", 0)),
                    stat=str(raw.get("stat", "mean")),
                )
            )
        else:
            budgets.append(
                ApprovedBudget(metric_key=str(key), ceiling=float(raw))
            )
    return tuple(budgets)


def check_budgets(
    aggregation: Aggregation,
    budgets: tuple[ApprovedBudget, ...],
) -> BudgetResult:
    """Compare per-metric means against approved ceilings."""
    violations: list[BudgetViolation] = []
    for budget in budgets:
        metric = aggregation.metrics.get(budget.metric_key)
        if metric is None:
            continue
        if budget.stat == "p95":
            actual = metric.p95
        else:
            actual = metric.mean
        if actual is None:
            continue
        if actual > budget.ceiling:
            violations.append(
                BudgetViolation(
                    metric_key=budget.metric_key,
                    ceiling=budget.ceiling,
                    actual=actual,
                    stat=budget.stat,
                )
            )
    return BudgetResult(violations=tuple(violations))


__all__ = [
    "ApprovedBudget",
    "BudgetResult",
    "BudgetViolation",
    "check_budgets",
    "load_budgets",
]
