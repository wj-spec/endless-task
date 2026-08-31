"""Offline automated evaluation layer (quality / regression).

Read-side capability over the v2 Runtime event journal + replay service. It
scores recorded runs deterministically and aggregates them into regression
baselines. It never writes to the runtime tables and never executes tools.
"""

from .aggregator import DEFAULT_BLOCKING_KEYS, aggregate, diff
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

__all__ = [
    "Aggregation",
    "ApprovalGateEvaluator",
    "BaselineDiff",
    "CompletionEvaluator",
    "DEFAULT_BLOCKING_KEYS",
    "DEFAULT_EVALUATORS",
    "DEFAULT_READ_ONLY_TOOLS",
    "DEFAULT_WRITE_TOOLS",
    "EfficiencyEvaluator",
    "EvalBatchRow",
    "EvalHarvestSpec",
    "EvalMetric",
    "EvalSeverity",
    "EvalUnit",
    "EvalVerdict",
    "EvaluationService",
    "LoopDetectorEvaluator",
    "MetricAggregation",
    "MetricDelta",
    "RobustnessEvaluator",
    "RunEvaluation",
    "RunEvaluationContext",
    "ScoreCard",
    "SqliteEvalRepository",
    "ToolCorrectnessEvaluator",
    "aggregate",
    "build_score_card",
    "diff",
    "harvest_runs",
    "summary_lines",
    "to_jsonl",
    "to_markdown",
]
