"""Runtime v2 运行时指标采集（进程内聚合，本地诊断用）。

覆盖发布级可观测性(P1-3)的最小集合:

- Run:状态分布、时长、ModelTurn 数、token 消耗。
- ModelTurn:首 token 延迟、总时长、finish reason。
- 上下文压缩:触发次数、释放 token、covered entry 数(数据源 v2_context_compactions)。
- 审批:请求数、平均等待时长。
- 前缀稳定度:同一 conversation 相邻 Run 的 system 前缀逐字一致率
  (Phase A 分层缓存的代理指标;前缀一致意味着前缀缓存可命中)。

指标为进程内内存聚合,重启丢失;定位是本地诊断,不做持久化。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ModelTurnMetric:
    run_id: str
    turn_index: int
    first_token_latency_ms: Optional[int]
    duration_ms: int
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    finish_reason: str


@dataclass(frozen=True)
class RunMetric:
    run_id: str
    conversation_id: str
    status: str
    duration_ms: int
    model_turn_count: int
    input_tokens: int
    output_tokens: int
    compacted: bool
    compaction_released_tokens: int


@dataclass(frozen=True)
class ApprovalMetric:
    run_id: str
    approval_id: str
    wait_ms: int
    decision: str


@dataclass(frozen=True)
class _PrefixFingerprintRecord:
    conversation_id: str
    fingerprint: str
    occurred_at: float


class RuntimeV2MetricsCollector:
    """进程内指标聚合器;线程安全由 GIL + 单事件循环保证。"""

    def __init__(self, *, max_runs: int = 1_000, max_turns: int = 5_000) -> None:
        self._runs: dict[str, RunMetric] = {}
        self._turns: list[ModelTurnMetric] = []
        self._approvals: list[ApprovalMetric] = []
        self._prefix_records: list[_PrefixFingerprintRecord] = []
        self._compaction_events = 0
        self._max_runs = max_runs
        self._max_turns = max_turns

    # ---------- 采集 ----------

    def record_run(self, metric: RunMetric) -> None:
        if len(self._runs) >= self._max_runs:
            # 简单淘汰:丢弃最早的 25%。
            discard = max(1, len(self._runs) // 4)
            for key in list(self._runs)[:discard]:
                self._runs.pop(key, None)
        self._runs[metric.run_id] = metric

    def record_model_turn(self, metric: ModelTurnMetric) -> None:
        self._turns.append(metric)
        if len(self._turns) > self._max_turns:
            self._turns = self._turns[-self._max_turns :]

    def record_approval(self, metric: ApprovalMetric) -> None:
        self._approvals.append(metric)
        if len(self._approvals) > 1_000:
            self._approvals = self._approvals[-1_000:]

    def record_compaction(self, *, released_tokens: int) -> None:
        self._compaction_events += 1

    def record_prefix_fingerprint(
        self,
        *,
        conversation_id: str,
        fingerprint: str,
    ) -> None:
        self._prefix_records.append(
            _PrefixFingerprintRecord(
                conversation_id=conversation_id,
                fingerprint=fingerprint,
                occurred_at=time.monotonic(),
            )
        )
        if len(self._prefix_records) > 2_000:
            self._prefix_records = self._prefix_records[-2_000:]

    # ---------- 汇总 ----------

    def summary(self) -> dict[str, object]:
        runs = list(self._runs.values())
        run_counts: dict[str, int] = {}
        total_duration_ms = 0
        total_input_tokens = 0
        total_output_tokens = 0
        compacted_runs = 0
        total_released = 0
        for run in runs:
            run_counts[run.status] = run_counts.get(run.status, 0) + 1
            total_duration_ms += run.duration_ms
            total_input_tokens += run.input_tokens
            total_output_tokens += run.output_tokens
            if run.compacted:
                compacted_runs += 1
                total_released += run.compaction_released_tokens

        first_token_latencies = [
            turn.first_token_latency_ms
            for turn in self._turns
            if turn.first_token_latency_ms is not None
        ]
        turn_durations = [turn.duration_ms for turn in self._turns]
        approval_waits = [approval.wait_ms for approval in self._approvals]

        return {
            "runs": {
                "count": len(runs),
                "byStatus": run_counts,
                "avgDurationMs": _avg(total_duration_ms, len(runs)),
                "totalInputTokens": total_input_tokens,
                "totalOutputTokens": total_output_tokens,
                "compactedRuns": compacted_runs,
                "compactionReleasedTokens": total_released,
            },
            "modelTurns": {
                "count": len(self._turns),
                "avgFirstTokenLatencyMs": _avg_list(first_token_latencies),
                "avgDurationMs": _avg_list(turn_durations),
                "finishReasons": _counts(turn.finish_reason for turn in self._turns),
            },
            "approvals": {
                "count": len(self._approvals),
                "avgWaitMs": _avg_list(approval_waits),
                "decisions": _counts(approval.decision for approval in self._approvals),
            },
            "compactions": {
                "events": self._compaction_events,
            },
            "prefixStability": self._prefix_stability(),
        }

    def _prefix_stability(self) -> dict[str, object]:
        """相邻 Run 前缀一致率:同一 conversation 连续两条记录的指纹相同即命中。"""
        if len(self._prefix_records) < 2:
            return {"comparisons": 0, "stableRate": None}
        by_conversation: dict[str, list[str]] = {}
        for record in self._prefix_records:
            by_conversation.setdefault(record.conversation_id, []).append(
                record.fingerprint
            )
        comparisons = 0
        stable = 0
        for fingerprints in by_conversation.values():
            for previous, current in zip(fingerprints, fingerprints[1:]):
                comparisons += 1
                if previous == current:
                    stable += 1
        return {
            "comparisons": comparisons,
            "stableRate": (stable / comparisons) if comparisons else None,
        }


def _avg(total: int, count: int) -> Optional[float]:
    return round(total / count, 1) if count else None


def _avg_list(values: list[int]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _counts(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts
