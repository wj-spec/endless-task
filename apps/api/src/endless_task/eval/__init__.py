"""Offline automated evaluation layer (quality / regression).

Read-side capability over the v2 Runtime event journal + replay service. It
scores recorded runs deterministically and aggregates them into regression
baselines. It never writes to the runtime tables and never executes tools.
"""

from .aggregator import DEFAULT_BLOCKING_KEYS, aggregate, diff
from .gate import EvalBaseline, GateResult, Waiver, gate_to_json, gate_to_markdown, run_gate
from .harvest import EvalHarvestSpec, harvest_runs
from .evaluators import (
    ApprovalGateEvaluator,
    CompletionEvaluator,
    DEFAULT_EVALUATORS,
    DEFAULT_READ_ONLY_TOOLS,
    DEFAULT_WRITE_TOOLS,
    EfficiencyEvaluator,
    LoopDetectorEvaluator,
    RobustnessEvaluator,
    RunEvaluationContext,
    ToolCorrectnessEvaluator,
    build_score_card,
)
from .models import (
    Aggregation,
    BaselineDiff,
    EvalMetric,
    EvalSeverity,
    EvalUnit,
    EvalVerdict,
    MetricAggregation,
    MetricDelta,
    RunEvaluation,
    ScoreCard,
)
from .reporting import summary_lines, to_jsonl, to_markdown
from .service import EvaluationService
from .storage import EvalBatchRow, SqliteEvalRepository
from .suites import (
    CHANGE_GATES,
    ChangeGate,
    EvalSuite,
    KNOWN_METRIC_KEYS,
    PENDING_SUITE_NAMES,
    SUITE_CATALOG,
    suite_names_for_change,
    suites_for_change,
)
from .trajectory_eval import evaluate_trajectories, evaluate_trajectory

__all__ = [
    "Aggregation",
    "ApprovalGateEvaluator",
    "BaselineDiff",
    "CHANGE_GATES",
    "ChangeGate",
    "CompletionEvaluator",
    "DEFAULT_BLOCKING_KEYS",
    "DEFAULT_EVALUATORS",
    "DEFAULT_READ_ONLY_TOOLS",
    "DEFAULT_WRITE_TOOLS",
    "EfficiencyEvaluator",
    "EvalBaseline",
    "EvalBatchRow",
    "EvalHarvestSpec",
    "EvalMetric",
    "EvalSeverity",
    "EvalSuite",
    "EvalUnit",
    "EvalVerdict",
    "EvaluationService",
    "GateResult",
    "KNOWN_METRIC_KEYS",
    "LoopDetectorEvaluator",
    "MetricAggregation",
    "MetricDelta",
    "PENDING_SUITE_NAMES",
    "RobustnessEvaluator",
    "RunEvaluation",
    "RunEvaluationContext",
    "SUITE_CATALOG",
    "ScoreCard",
    "SqliteEvalRepository",
    "ToolCorrectnessEvaluator",
    "Waiver",
    "aggregate",
    "build_score_card",
    "diff",
    "evaluate_trajectories",
    "evaluate_trajectory",
    "gate_to_json",
    "gate_to_markdown",
    "harvest_runs",
    "run_gate",
    "suite_names_for_change",
    "suites_for_change",
    "summary_lines",
    "to_jsonl",
    "to_markdown",
]
