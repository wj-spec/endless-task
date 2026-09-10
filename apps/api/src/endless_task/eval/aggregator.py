from __future__ import annotations

from typing import Mapping, Sequence

from .models import (
    Aggregation,
    BaselineDiff,
    EvalSeverity,
    EvalUnit,
    MetricAggregation,
    MetricDelta,
    RunEvaluation,
    ScoreCard,
)


# Keys that, when they regress, should fail a CI gate. Default is the
# correctness/safety set; expand in the CLI or tests as needed.
DEFAULT_BLOCKING_KEYS = frozenset(
    {"completion", "approval_gate", "loop_detected", "robustness"}
)


def _numeric(value: int | float | bool) -> float:
    return 1.0 if isinstance(value, bool) and value else float(value)


def _is_worse_when_larger(unit: EvalUnit) -> bool:
    # BOOL metrics are "pass" oriented: a higher pass_rate is better, so a lower
    # value is a regression. Count/token/ms metrics are cost oriented: higher is
    # worse.
    return unit is not EvalUnit.BOOL


def aggregate(run_evaluations: Sequence[RunEvaluation | ScoreCard]) -> Aggregation:
    """Aggregate per-run results (repo :class:`RunEvaluation` or trajectory
    :class:`ScoreCard`) into one :class:`Aggregation`."""
    by_key: dict[str, list[float]] = {}
    blocker_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    pass_rates: dict[str, int] = {}
    metric_units: dict[str, EvalUnit] = {}

    pass_count = warn_count = fail_count = judge_unavailable_count = 0

    for run in run_evaluations:
        # Normalize carriers: RunEvaluation exposes .score_card/.verdict;
        # ScoreCard is itself the carrier.
        score_card = run.score_card if hasattr(run, "score_card") else run
        verdict = run.verdict if hasattr(run, "verdict") else score_card.verdict
        if verdict.value == "pass":
            pass_count += 1
        elif verdict.value == "warn":
            warn_count += 1
        elif verdict.value == "fail":
            fail_count += 1
        if score_card.judge_unavailable:
            judge_unavailable_count += 1

        for metric in score_card.metrics:
            if metric.value is None:
                continue
            key = metric.key
            metric_units[key] = metric.unit
            by_key.setdefault(key, []).append(_numeric(metric.value))
            if metric.severity is EvalSeverity.BLOCKER:
                blocker_counts[key] = blocker_counts.get(key, 0) + 1
            if metric.severity is EvalSeverity.WARNING:
                warning_counts[key] = warning_counts.get(key, 0) + 1
            if isinstance(metric.value, bool):
                pass_rates[key] = pass_rates.get(key, 0) + (1 if metric.value else 0)

    metrics: dict[str, MetricAggregation] = {}
    for key, values in by_key.items():
        count = len(values)
        total = sum(values)
        unit = metric_units[key]
        is_bool = unit is EvalUnit.BOOL
        mean = total / count if count else None
        if is_bool:
            pass_rate = pass_rates.get(key, 0) / count if count else None
        else:
            pass_rate = None
        p95 = None
        if not is_bool and values:
            import math

            ordered = sorted(values)
            p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
        metrics[key] = MetricAggregation(
            key=key,
            count=count,
            mean=round(mean, 4) if mean is not None else None,
            min=round(min(values), 4) if values else None,
            max=round(max(values), 4) if values else None,
            p95=round(p95, 4) if p95 is not None else None,
            pass_rate=round(pass_rate, 4) if pass_rate is not None else None,
            blocker_count=blocker_counts.get(key, 0),
            warning_count=warning_counts.get(key, 0),
        )

    return Aggregation(
        run_count=len(run_evaluations),
        metrics=metrics,
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        judge_unavailable_count=judge_unavailable_count,
    )


def _effective_value(aggregate_metrics: MetricAggregation) -> float:
    # Use pass_rate for BOOL metrics, mean otherwise.
    if aggregate_metrics.pass_rate is not None:
        return aggregate_metrics.pass_rate
    if aggregate_metrics.mean is not None:
        return aggregate_metrics.mean
    return 0.0


def diff(
    baseline: Aggregation,
    candidate: Aggregation,
    *,
    tolerances: Mapping[str, float] | None = None,
    blocking_keys: Sequence[str] = (),
) -> BaselineDiff:
    """Compare a candidate batch against a baseline and flag regressions.

    A metric is ``regressed`` when it moved in the worse direction by more than
    its tolerance: BOOL pass-rate metrics are worse when they drop; count / token
    / ms metrics are worse when they rise. A regression is ``blocking`` when its
    key is in ``blocking_keys`` and it exceeded tolerance.
    """
    if not blocking_keys:
        blocking_keys = DEFAULT_BLOCKING_KEYS
    tolerances = tolerances or {}

    baseline_pass_rate = (
        baseline.pass_count / baseline.run_count if baseline.run_count else 0.0
    )
    candidate_pass_rate = (
        candidate.pass_count / candidate.run_count if candidate.run_count else 0.0
    )

    deltas: list[MetricDelta] = []
    blocking: list[MetricDelta] = []
    keys = candidate.metrics.keys() | baseline.metrics.keys()
    for key in keys:
        baseline_metric = baseline.metrics.get(key)
        candidate_metric = candidate.metrics.get(key)
        if baseline_metric is None or candidate_metric is None:
            # A metric present in only one batch is not a comparable regression.
            continue
        tolerance = tolerances.get(key, 0.0)
        base_value = _effective_value(baseline_metric)
        cand_value = _effective_value(candidate_metric)
        unit = _unit_for(key, baseline_metric)
        worse_when_larger = _is_worse_when_larger(unit)
        delta = cand_value - base_value
        if worse_when_larger:
            regressed = delta > tolerance
        else:
            regressed = delta < -tolerance
        metric_delta = MetricDelta(
            key=key,
            baseline_value=round(base_value, 4),
            candidate_value=round(cand_value, 4),
            delta=round(delta, 4),
            regressed=regressed,
        )
        deltas.append(metric_delta)
        if regressed and key in blocking_keys:
            blocking.append(metric_delta)

    return BaselineDiff(
        baseline_total_pass_rate=round(baseline_pass_rate, 4),
        candidate_total_pass_rate=round(candidate_pass_rate, 4),
        deltas=tuple(deltas),
        blocking_regressions=tuple(blocking),
    )


def _unit_for(key: str, metric: MetricAggregation) -> EvalUnit:
    # Preserve the unit by re-deriving from value semantics is not possible from
    # the aggregation alone; callers keep units via the BOOL/pass_rate heuristic.
    if metric.pass_rate is not None:
        return EvalUnit.BOOL
    # Count-ish keys are cost-oriented; a single shared unit is fine for diff.
    return EvalUnit.COUNT
