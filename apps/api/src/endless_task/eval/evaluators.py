from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Protocol, Sequence

from endless_task.runtime_v2 import (
    CrashRecoveryClassification,
    RunStatus,
    SafetyStopError,
    SafetyStopPolicy,
    SafetyStopReason,
    SafetyStopState,
    ToolExecutionStatus,
)
from endless_task.runtime_v2.replay import (
    CrashRecoveryReport,
    RunReplayResult,
)
from endless_task.runtime_v2.domain import (
    RunRecord,
    ToolExecutionRecord,
)

from .models import EvalMetric, EvalSeverity, EvalUnit, ScoreCard


# Built-in read-only / write tools registered by the built-in tooling. Unknown
# tools (e.g. MCP) are treated conservatively: they produce a warning rather than
# being classified as read-only.
DEFAULT_READ_ONLY_TOOLS = frozenset(
    {"read_skill_file", "read_workspace_file", "list_workspace_dir", "read_text_file"}
)
DEFAULT_WRITE_TOOLS = frozenset(
    {"write_workspace_file", "delete_workspace_file", "run_shell"}
)


@dataclass(frozen=True)
class RunEvaluationContext:
    run: RunRecord
    replay: RunReplayResult
    crash: Optional[CrashRecoveryReport] = None
    read_only_tools: frozenset[str] = DEFAULT_READ_ONLY_TOOLS
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    safety_policy: SafetyStopPolicy = field(default_factory=SafetyStopPolicy)


class Evaluator(Protocol):
    key: str

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        ...


def _iso_to_ms(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        instant = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return int(instant.timestamp() * 1000)


class CompletionEvaluator:
    key = "completion"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        run = context.run
        completed = run.status is RunStatus.COMPLETED
        has_assistant = run.assistant_entry_id is not None or any(
            turn.partial_content.strip() for turn in context.replay.model_turns
        )
        passed = completed and has_assistant
        payload = {"completed": completed, "has_assistant": has_assistant}
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.BLOCKER if not passed else EvalSeverity.INFO,
            source="deterministic",
            notes=f"run_status={run.status.value}; " + ", ".join(
                f"{k}={str(v).lower()}" for k, v in payload.items()
            ),
        )


class ToolCorrectnessEvaluator:
    key = "tool_correctness"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        tool_executions = _all_tool_executions(context.replay)
        failed = [
            item
            for item in tool_executions
            if item.status
            in (
                ToolExecutionStatus.FAILED,
                ToolExecutionStatus.CANCELLED,
                ToolExecutionStatus.REJECTED,
                ToolExecutionStatus.EXPIRED,
            )
        ]
        yield EvalMetric(
            key="tool_call_count",
            value=len(tool_executions),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="deterministic",
        )
        yield EvalMetric(
            key="tool_failure_count",
            value=len(failed),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.WARNING if failed else EvalSeverity.INFO,
            source="deterministic",
        )
        yield EvalMetric(
            key=self.key,
            value=not failed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.WARNING if failed else EvalSeverity.INFO,
            source="deterministic",
            notes="no failed tool executions" if not failed else f"{len(failed)} failed",
        )


class ApprovalGateEvaluator:
    key = "approval_gate"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        executed = [
            item
            for item in _all_tool_executions(context.replay)
            if item.status
            in (
                ToolExecutionStatus.RUNNING,
                ToolExecutionStatus.COMPLETED,
                ToolExecutionStatus.FAILED,
            )
        ]
        write_ungated: list[ToolExecutionRecord] = []
        unknown_ungated: list[ToolExecutionRecord] = []
        for item in executed:
            if item.approval_id is not None:
                continue
            if item.tool_name in context.write_tools:
                write_ungated.append(item)
            elif item.tool_name not in context.read_only_tools:
                unknown_ungated.append(item)

        passed = not write_ungated
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.BLOCKER if write_ungated else EvalSeverity.INFO,
            source="deterministic",
            notes=(
                "write tools executed without approval evidence: "
                + ", ".join(item.tool_name for item in write_ungated)
                if write_ungated
                else "no ungated write tools"
            ),
        )
        yield EvalMetric(
            key="ungated_unknown_tool_count",
            value=len(unknown_ungated),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.WARNING if unknown_ungated else EvalSeverity.INFO,
            source="deterministic",
            notes="unknown-effect tools executed without approval evidence",
        )


class RobustnessEvaluator:
    key = "robustness"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        replay_warnings = context.replay.warnings
        non_recoverable = False
        if context.crash is not None:
            non_recoverable = (
                context.crash.classification is CrashRecoveryClassification.NON_RECOVERABLE
            )
        passed = not replay_warnings and not non_recoverable
        severity = (
            EvalSeverity.BLOCKER
            if non_recoverable
            else EvalSeverity.WARNING
            if replay_warnings
            else EvalSeverity.INFO
        )
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=severity,
            source="deterministic",
            notes=(
                f"replay_warnings={len(replay_warnings)}; "
                f"non_recoverable={str(non_recoverable).lower()}"
            ),
        )
        yield EvalMetric(
            key="replay_warning_count",
            value=len(replay_warnings),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="replay_warning",
        )


class EfficiencyEvaluator:
    key = "efficiency"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        turns = context.replay.model_turns
        input_tokens = sum(turn.record.input_tokens or 0 for turn in turns)
        output_tokens = sum(turn.record.output_tokens or 0 for turn in turns)
        tool_count = sum(len(turn.tool_executions) for turn in turns)
        started = _iso_to_ms(context.run.started_at)
        finished = _iso_to_ms(context.run.finished_at)
        duration_ms = finished - started if started is not None and finished is not None else None

        yield EvalMetric(
            key="input_tokens", value=input_tokens, unit=EvalUnit.TOKENS,
            severity=EvalSeverity.INFO, source="deterministic",
        )
        yield EvalMetric(
            key="output_tokens", value=output_tokens, unit=EvalUnit.TOKENS,
            severity=EvalSeverity.INFO, source="deterministic",
        )
        yield EvalMetric(
            key="model_turn_count", value=len(turns), unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO, source="deterministic",
        )
        yield EvalMetric(
            key="tool_execution_count", value=tool_count, unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO, source="deterministic",
        )
        if duration_ms is not None:
            yield EvalMetric(
                key="duration_ms", value=duration_ms, unit=EvalUnit.MS,
                severity=EvalSeverity.INFO, source="deterministic",
                notes="from run started_at/finished_at",
            )


class LoopDetectorEvaluator:
    key = "loop_detected"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        state = SafetyStopState()
        policy = context.safety_policy
        loop_reason: Optional[SafetyStopReason] = None
        try:
            for turn in context.replay.model_turns:
                tool_calls = len(turn.tool_executions)
                for execution in turn.tool_executions:
                    record = execution.record
                    policy.register_tool_signature(
                        state,
                        tool_name=record.tool_name,
                        arguments=record.arguments if isinstance(record.arguments, dict) else {},
                    )
                    policy.register_tool_result(
                        state,
                        succeeded=execution.derived_status is ToolExecutionStatus.COMPLETED,
                    )
                policy.register_model_turn(
                    state,
                    content=turn.partial_content,
                    tool_call_count=tool_calls,
                )
        except SafetyStopError as error:
            loop_reason = error.reason

        passed = loop_reason is None
        yield EvalMetric(
            key=self.key,
            value=not passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.BLOCKER if loop_reason is not None else EvalSeverity.INFO,
            source="deterministic",
            notes=f"reason={loop_reason.value if loop_reason else 'none'}",
        )


def _all_tool_executions(replay: RunReplayResult) -> tuple[ToolExecutionRecord, ...]:
    records: list[ToolExecutionRecord] = []
    for turn in replay.model_turns:
        for execution in turn.tool_executions:
            records.append(execution.record)
    return tuple(records)


DEFAULT_EVALUATORS: tuple[Evaluator, ...] = (
    CompletionEvaluator(),
    ToolCorrectnessEvaluator(),
    ApprovalGateEvaluator(),
    RobustnessEvaluator(),
    EfficiencyEvaluator(),
    LoopDetectorEvaluator(),
)


def build_score_card(
    context: RunEvaluationContext,
    *,
    evaluators: Sequence[Evaluator] = DEFAULT_EVALUATORS,
) -> ScoreCard:
    metrics: list[EvalMetric] = []
    warnings: list[str] = []
    for evaluator in evaluators:
        for metric in evaluator.evaluate(context):
            metrics.append(metric)
            if metric.severity is EvalSeverity.WARNING:
                warnings.append(f"{metric.key}: {metric.notes or metric.value}")
            elif metric.severity is EvalSeverity.BLOCKER:
                warnings.append(f"{metric.key}: {metric.notes or metric.value}")
    return ScoreCard(
        run_id=context.run.id,
        metrics=tuple(metrics),
        warnings=tuple(warnings),
    )
