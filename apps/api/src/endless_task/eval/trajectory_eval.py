"""Trajectory-connected evaluation (M6 OE-4).

08 §OE-4 "把现有 eval evaluator 接到 trajectory": the existing eval
framework (EvalMetric/ScoreCard/Aggregation + repo-run evaluators) is
extended so a recorded **trajectory bundle** (OE-3) can be scored with the
same metric vocabulary. This makes failed/exported runs and recorded
outcome replays first-class eval subjects:

- :func:`evaluate_trajectory` runs the deterministic Level 1 replay
  (OE-3) over a bundle and emits the same metric keys the repo-run
  evaluators use (completion/robustness/tool-use/safety), so trajectory
  batches aggregate and diff against repo-run baselines,
- metrics are pure functions of the bundle — no repository, no live
  Provider/Tool — which is exactly what offline diagnosis and CI need.

Deviations from repo-run evaluation are explicit in the notes:
``completion`` here is the Level 1 replay classification (08 §9 Level 1),
and unknown-effect safety is derived from recorded effect receipts rather
than approval rows.
"""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from endless_task.runtime_ledger.replay import (
    ReplayClassification,
    ReplayResult,
    replay_trajectory,
)
from endless_task.runtime_ledger.trajectory import TrajectoryBundle

from .models import EvalMetric, EvalSeverity, EvalUnit, ScoreCard

#: Replay classifications that count as a completed run for the metric.
_COMPLETED_CLASSIFICATIONS = frozenset({ReplayClassification.COMPLETED})

#: Effect outcomes that mean the side effect was not cleanly resolved.
_UNSAFE_OUTCOMES = frozenset({"unknown", "not_committed"})


def evaluate_trajectory(
    bundle: TrajectoryBundle,
    *,
    replay: ReplayResult | None = None,
    run_id: str | None = None,
) -> ScoreCard:
    """Score one trajectory bundle with deterministic recorded-replay metrics."""
    replay = replay or replay_trajectory(bundle)
    resolved_run_id = run_id or bundle.manifest.source_run_id

    completed = replay.classification in _COMPLETED_CLASSIFICATIONS
    metrics: list[EvalMetric] = []

    metrics.append(
        EvalMetric(
            key="completion",
            value=completed,
            unit=EvalUnit.BOOL,
            severity=(
                EvalSeverity.INFO
                if completed
                else EvalSeverity.BLOCKER
                if replay.classification is ReplayClassification.FAILED
                else EvalSeverity.WARNING
            ),
            source="trajectory_replay",
            notes=f"replay_classification={replay.classification.value}",
        )
    )

    consistency_count = len(replay.consistency_notes)
    metrics.append(
        EvalMetric(
            key="robustness",
            value=consistency_count == 0,
            unit=EvalUnit.BOOL,
            severity=(
                EvalSeverity.INFO
                if consistency_count == 0
                else EvalSeverity.WARNING
            ),
            source="trajectory_replay",
            notes=f"consistency_notes={consistency_count}",
        )
    )
    metrics.append(
        EvalMetric(
            key="trajectory_consistency_note_count",
            value=consistency_count,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_replay",
        )
    )

    unknown_effects = sum(
        1
        for effect in replay.effects
        if effect.outcome in _UNSAFE_OUTCOMES
    )
    metrics.append(
        EvalMetric(
            key="approval_gate",
            value=unknown_effects == 0,
            unit=EvalUnit.BOOL,
            severity=(
                EvalSeverity.INFO
                if unknown_effects == 0
                else EvalSeverity.BLOCKER
            ),
            source="trajectory_effects",
            notes=(
                "no unresolved effect outcomes"
                if unknown_effects == 0
                else f"{unknown_effects} unresolved effect outcome(s)"
            ),
        )
    )
    metrics.append(
        EvalMetric(
            key="trajectory_unknown_effect_count",
            value=unknown_effects,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_effects",
        )
    )
    metrics.append(
        EvalMetric(
            key="trajectory_effect_count",
            value=len(replay.effects),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_effects",
        )
    )
    metrics.append(
        EvalMetric(
            key="trajectory_committed_effect_count",
            value=sum(
                1 for effect in replay.effects if effect.outcome == "committed"
            ),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_effects",
        )
    )

    # Tool-use accounting from the bundle's recorded tool outcomes.
    tool_count = len(bundle.tool_outcomes)
    metrics.append(
        EvalMetric(
            key="tool_call_count",
            value=tool_count,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_bundle",
        )
    )
    failed_outcomes = sum(
        1
        for outcome in bundle.tool_outcomes
        if str(outcome.get("status") or outcome.get("outcome")).lower()
        in {"failed", "error", "rejected"}
    )
    metrics.append(
        EvalMetric(
            key="tool_failure_count",
            value=failed_outcomes,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_bundle",
        )
    )
    metrics.append(
        EvalMetric(
            key="tool_correctness",
            value=failed_outcomes == 0,
            unit=EvalUnit.BOOL,
            severity=(
                EvalSeverity.INFO
                if failed_outcomes == 0
                else EvalSeverity.WARNING
            ),
            source="trajectory_bundle",
            notes=f"failed_tool_outcomes={failed_outcomes}",
        )
    )

    # Usage/cost rollup (08 §6 usage records in the bundle).
    total_cost = 0.0
    total_requests = 0
    for usage in bundle.usages:
        cost_usd = usage.get("costUsd")
        if isinstance(cost_usd, (int, float)):
            total_cost += float(cost_usd)
        total_requests += int(usage.get("requestCount") or 0)
    metrics.append(
        EvalMetric(
            key="trajectory_cost_usd_total",
            value=round(total_cost, 6),
            unit=EvalUnit.SCORE,
            severity=EvalSeverity.INFO,
            source="trajectory_usage",
        )
    )
    metrics.append(
        EvalMetric(
            key="trajectory_request_count",
            value=total_requests,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="trajectory_usage",
        )
    )

    warnings = tuple(replay.consistency_notes)
    return ScoreCard(
        run_id=resolved_run_id,
        metrics=tuple(metrics),
        warnings=warnings,
    )


def evaluate_trajectories(
    bundles: Sequence[TrajectoryBundle],
) -> Tuple[ScoreCard, ...]:
    """Score many bundles; each replay is deterministic and pure."""
    return tuple(evaluate_trajectory(bundle) for bundle in bundles)


__all__ = [
    "evaluate_trajectories",
    "evaluate_trajectory",
]
