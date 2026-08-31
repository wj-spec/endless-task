from __future__ import annotations

import json
from io import StringIO
from typing import Sequence

from .models import Aggregation, RunEvaluation

_METRIC_ORDER = (
    "completion",
    "tool_correctness",
    "approval_gate",
    "robustness",
    "loop_detected",
    "tool_failure_count",
    "ungated_unknown_tool_count",
    "replay_warning_count",
    "input_tokens",
    "output_tokens",
    "model_turn_count",
    "tool_execution_count",
    "duration_ms",
)


def to_jsonl(run_evaluations: Sequence[RunEvaluation]) -> str:
    return "\n".join(
        json.dumps(run.to_dict(), ensure_ascii=False, sort_keys=True)
        for run in run_evaluations
    )


def to_markdown(
    run_evaluations: Sequence[RunEvaluation],
    aggregation: Aggregation,
) -> str:
    buffer = StringIO()
    buffer.write("## Run evaluations\n\n")
    buffer.write("| verdict | run_id | status | blockers | warnings |\n")
    buffer.write("|---|---|---|---|---|\n")
    for run in run_evaluations:
        buffer.write(
            f"| {run.verdict.value} | {run.run_id} | {run.run_status.value} "
            f"| {int(run.score_card.has_blocker)} "
            f"| {run.score_card.warning_count} |\n"
        )
    buffer.write("\n## Aggregated metrics\n\n")
    buffer.write("| metric | count | mean | min | max | pass_rate | blockers |\n")
    buffer.write("|---|---|---|---|---|---|---|\n")
    keys = [key for key in _METRIC_ORDER if key in aggregation.metrics]
    keys += [key for key in aggregation.metrics if key not in keys]
    for key in keys:
        metric = aggregation.metrics[key]
        fmt_mean = _fmt(metric.mean)
        fmt_pass = _fmt(metric.pass_rate) if metric.pass_rate is not None else "-"
        buffer.write(
            f"| {key} | {metric.count} | {fmt_mean} | {_fmt(metric.min)} "
            f"| {_fmt(metric.max)} | {fmt_pass} | {metric.blocker_count} |\n"
        )
    return buffer.getvalue()


def summary_lines(aggregation: Aggregation) -> list[str]:
    lines = [
        f"runs={aggregation.run_count}",
        (
            f"verdicts: pass={aggregation.pass_count} "
            f"warn={aggregation.warn_count} fail={aggregation.fail_count}"
        ),
        f"judge_unavailable={aggregation.judge_unavailable_count}",
    ]
    for key in _METRIC_ORDER:
        metric = aggregation.metrics.get(key)
        if metric is None:
            continue
        value = (
            f"pass_rate={_fmt(metric.pass_rate)}"
            if metric.pass_rate is not None
            else f"mean={_fmt(metric.mean)}"
        )
        blocker = f" blockers={metric.blocker_count}" if metric.blocker_count else ""
        warning = f" warnings={metric.warning_count}" if metric.warning_count else ""
        lines.append(f"{key}: count={metric.count} {value}{blocker}{warning}")
    return lines


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)
