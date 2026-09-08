from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from endless_task.runtime_v2 import (
    CrashRecoveryClassification,
    RunStatus,
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


# Built-in read-only tools (auto-executed). Unknown tools (e.g. MCP) are treated
# conservatively: they produce a warning rather than being classified as read-only.
DEFAULT_READ_ONLY_TOOLS = frozenset(
    {"read_skill_file", "read_workspace_file", "list_workspace_dir", "read_text_file"}
)
# 已知「自动执行」的写入工具（与 pi 对齐，写入已绑定工作区无需逐次确认）。
# 评估时视作已知工具，不再当作 unknown-effect 触发 warning。
DEFAULT_AUTO_WRITE_TOOLS = frozenset({"write_workspace_file"})
# Tools that still REQUIRE explicit approval (destructive / external action outside the
# bound workspace). delete_workspace_file / run_shell 仍须确认。
DEFAULT_WRITE_TOOLS = frozenset(
    {"delete_workspace_file", "run_shell"}
)


@dataclass(frozen=True)
class MemoryWriteFact:
    """D1：一次记忆写入的可判定事实（来源/状态）。"""

    memory_id: str
    has_source: bool = True
    status: str = "active"


@dataclass(frozen=True)
class ReflectionFact:
    """D1：一条反思洞见的可判定事实（来源/长度）。"""

    reflection_id: str
    trigger: str = ""
    has_sources: bool = True
    insight_length: int = 0
    status: str = "pending"


@dataclass(frozen=True)
class RunEvaluationContext:
    run: RunRecord
    replay: RunReplayResult
    crash: Optional[CrashRecoveryReport] = None
    #: how many context compactions happened on this run's lane (G1 item 6)
    compaction_count: int = 0
    read_only_tools: frozenset[str] = DEFAULT_READ_ONLY_TOOLS
    auto_write_tools: frozenset[str] = DEFAULT_AUTO_WRITE_TOOLS
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    #: D1：本轮真实注入的引用编号（turn_id -> {"K1", ...}），用于引用正确性。
    citation_labels_by_turn: Mapping[str, frozenset[str]] = field(
        default_factory=dict
    )
    #: D1：本轮写入的记忆（来源/状态）。
    memory_writes: tuple[MemoryWriteFact, ...] = ()
    #: D1：本轮产出的反思洞见。
    reflections: tuple[ReflectionFact, ...] = ()


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
            elif (
                item.tool_name not in context.read_only_tools
                and item.tool_name not in context.auto_write_tools
            ):
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
    """对已录制 Run 的 QA 诊断：检测是否出现病态循环。

    注意：这只是评估/CI 层的质量信号，**不约束运行时循环**。运行时（v2）已与 pi 对齐，
    不再以任何计数条件截停；模型负责自行收敛。此处仅对「录制结果」做诊断打分，用于
    baseline/candidate 回归对比，因此保留独立的重复签名/连续失败/空回检测，但内联实现，
    不再依赖运行时的 SafetyStop 类。
    """

    key = "loop_detected"

    # 诊断阈值（仅评估用，与运行时无关）。
    MAX_CONSECUTIVE_TOOL_FAILURES = 3
    MAX_CONSECUTIVE_EMPTY_MODEL_TURNS = 2

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        seen_signatures: set[tuple[str, str]] = set()
        consecutive_failed = 0
        consecutive_empty = 0
        loop_reason: Optional[str] = None
        for turn in context.replay.model_turns:
            tool_calls = len(turn.tool_executions)
            for execution in turn.tool_executions:
                record = execution.record
                arguments = (
                    record.arguments if isinstance(record.arguments, dict) else {}
                )
                canonical = json.dumps(
                    dict(arguments),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                signature = (record.tool_name, canonical)
                if signature in seen_signatures:
                    loop_reason = "duplicate_tool_signature"
                    break
                seen_signatures.add(signature)
                if execution.derived_status is ToolExecutionStatus.COMPLETED:
                    consecutive_failed = 0
                else:
                    consecutive_failed += 1
                    if consecutive_failed >= self.MAX_CONSECUTIVE_TOOL_FAILURES:
                        loop_reason = "consecutive_tool_failures"
                        break
            if loop_reason is not None:
                break
            if turn.partial_content.strip() or tool_calls:
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= self.MAX_CONSECUTIVE_EMPTY_MODEL_TURNS:
                    loop_reason = "empty_model_turns"
                    break

        passed = loop_reason is None
        yield EvalMetric(
            key=self.key,
            value=not passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.BLOCKER if loop_reason is not None else EvalSeverity.INFO,
            source="deterministic",
            notes=f"reason={loop_reason or 'none'}",
        )


class CitationCorrectnessEvaluator:
    """D1 引用正确性：回答里的 `[K1]` 必须命中本轮真实注入的引用。

    误标（模型杜撰或指向上文其他轮）会被计数；有任何未命中即判否，并按
    BLOCKER 处理——引用错配比"没有引用"更容易误导用户。
    """

    key = "citation_correctness"

    _MARKER_RE = re.compile(r"\[K(\d+)\]")

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        markers = 0
        unresolved = 0
        for turn in context.replay.model_turns:
            content = turn.partial_content or ""
            if not content:
                continue
            injected = context.citation_labels_by_turn.get(
                turn.record.id, frozenset()
            )
            for match in self._MARKER_RE.finditer(content):
                markers += 1
                if f"K{match.group(1)}" not in injected:
                    unresolved += 1
        passed = unresolved == 0
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO if passed else EvalSeverity.BLOCKER,
            source="deterministic",
            notes=f"markers={markers}, unresolved={unresolved}",
        )
        yield EvalMetric(
            key="citation_unresolved_count",
            value=unresolved,
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="deterministic",
        )


class MemoryQualityEvaluator:
    """D1 记忆质量：本轮写入的记忆是否可溯源、是否仍有效（参考指标）。"""

    key = "memory_quality"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        writes = context.memory_writes
        bad = [
            write
            for write in writes
            if not write.has_source or write.status != "active"
        ]
        passed = not bad
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO if passed else EvalSeverity.WARNING,
            source="deterministic",
            notes=(
                f"writes={len(writes)}, unsourced_or_inactive={len(bad)}"
            ),
        )
        yield EvalMetric(
            key="memory_write_count",
            value=len(writes),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="deterministic",
        )


class ReflectionQualityEvaluator:
    """D1 反思质量：洞见是否可溯源、是否有实质内容。"""

    key = "reflection_quality"

    #: 少于该长度的"洞见"视为没有实质内容。
    MIN_INSIGHT_LENGTH = 8

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        reflections = context.reflections
        bad = [
            reflection
            for reflection in reflections
            if not reflection.has_sources
            or reflection.insight_length < self.MIN_INSIGHT_LENGTH
        ]
        passed = not bad
        yield EvalMetric(
            key=self.key,
            value=passed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO if passed else EvalSeverity.WARNING,
            source="deterministic",
            notes=(
                f"reflections={len(reflections)}, "
                f"unsourced_or_thin={len(bad)}"
            ),
        )
        yield EvalMetric(
            key="reflection_count",
            value=len(reflections),
            unit=EvalUnit.COUNT,
            severity=EvalSeverity.INFO,
            source="deterministic",
        )


def _all_tool_executions(replay: RunReplayResult) -> tuple[ToolExecutionRecord, ...]:
    records: list[ToolExecutionRecord] = []
    for turn in replay.model_turns:
        for execution in turn.tool_executions:
            records.append(execution.record)
    return tuple(records)


class ContextCompactionEvaluator:
    """context_compacted: whether the run's lane underwent a context
    compaction — observable signal for the context-compaction eval topic."""

    key = "context_compacted"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        compacted = context.compaction_count > 0
        yield EvalMetric(
            key=self.key,
            value=compacted,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO,
            source="deterministic",
            notes=f"compaction_count={context.compaction_count}",
        )


class FailedRunEvaluator:
    """failed_run: whether this run reached FAILED (checkpoint/restore
    scenario population — a corpus with failed runs exercises restore)."""

    key = "failed_run"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        failed = context.run.status is RunStatus.FAILED
        yield EvalMetric(
            key=self.key,
            value=failed,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO,
            source="deterministic",
            notes=f"run_status={context.run.status.value}",
        )


class AutoRestoredEvaluator:
    """auto_restored: a FAILED run carries a run_auto_restored event
    (in-process observer or startup reconciliation actually rolled back its
    workspace). Failed runs without the marker mean the crash-window repair
    did not run — a regression signal for checkpoint/restore reliability."""

    key = "auto_restored"

    def evaluate(self, context: RunEvaluationContext) -> Iterable[EvalMetric]:
        failed = context.run.status is RunStatus.FAILED
        restored = any(
            event.event_type == "run_auto_restored"
            for event in context.replay.events
        )
        value = failed and restored
        yield EvalMetric(
            key=self.key,
            value=value,
            unit=EvalUnit.BOOL,
            severity=(
                EvalSeverity.BLOCKER
                if failed and not restored
                else EvalSeverity.INFO
            ),
            source="deterministic",
            notes=(
                f"failed={str(failed).lower()}; restored={str(restored).lower()}"
            ),
        )


DEFAULT_EVALUATORS: tuple[Evaluator, ...] = (
    ContextCompactionEvaluator(),
    FailedRunEvaluator(),
    AutoRestoredEvaluator(),
    CompletionEvaluator(),
    ToolCorrectnessEvaluator(),
    ApprovalGateEvaluator(),
    RobustnessEvaluator(),
    EfficiencyEvaluator(),
    LoopDetectorEvaluator(),
    CitationCorrectnessEvaluator(),
    MemoryQualityEvaluator(),
    ReflectionQualityEvaluator(),
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
