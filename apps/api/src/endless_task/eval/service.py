from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from endless_task.runtime_v2 import (
    RunStatus,
    RuntimeV2ReplayService,
)
from endless_task.runtime_v2.domain import RunRecord
from endless_task.storage import Database, SqliteRuntimeV2Repository

from .aggregator import aggregate
from .evaluators import (
    DEFAULT_READ_ONLY_TOOLS,
    DEFAULT_WRITE_TOOLS,
    MemoryWriteFact,
    ReflectionFact,
    RunEvaluationContext,
    build_score_card,
)
from .harvest import EvalHarvestSpec, harvest_runs
from .models import Aggregation, RunEvaluation
from .storage import SqliteEvalRepository

_ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.CREATED,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPACTING,
        RunStatus.CANCELLING,
    }
)


@dataclass
class EvaluationService:
    database: Database
    read_only_tools: frozenset[str] = DEFAULT_READ_ONLY_TOOLS
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    #: D1：可选注入，用于引用正确性/记忆/反思质量（缺失时这些指标只报空事实）。
    retrieval_event_repository: Optional[object] = None
    memory_repository: Optional[object] = None
    reflection_repository: Optional[object] = None
    _repository: SqliteRuntimeV2Repository = field(init=False)
    _replay: RuntimeV2ReplayService = field(init=False)
    _eval: SqliteEvalRepository = field(init=False)

    def __post_init__(self) -> None:
        self._repository = SqliteRuntimeV2Repository(self.database)
        self._replay = RuntimeV2ReplayService(self._repository)
        self._eval = SqliteEvalRepository(self.database)

    def evaluate_run(self, run: RunRecord) -> RunEvaluation:
        replay = self._replay.replay_run(run.id)
        crash = (
            self._replay.classify_crash_recovery(run.id)
            if run.status in _ACTIVE_RUN_STATUSES
            else None
        )
        try:
            compaction_count = len(
                self._repository.list_context_compactions(run.lane_id)
            )
        except Exception:
            compaction_count = 0
        context = RunEvaluationContext(
            run=run,
            replay=replay,
            crash=crash,
            compaction_count=compaction_count,
            read_only_tools=self.read_only_tools,
            write_tools=self.write_tools,
            citation_labels_by_turn=self._citation_labels(run),
            memory_writes=self._memory_writes(run),
            reflections=self._reflections(run),
        )
        score_card = build_score_card(context)
        return RunEvaluation(
            run_id=run.id,
            run_status=run.status,
            score_card=score_card,
            replay_warnings=replay.warnings,
        )

    # ---------- D1：事实采集（fail-open，缺仓库/缺数据一律返回空） ----------

    def _citation_labels(self, run: RunRecord) -> dict[str, frozenset[str]]:
        repository = self.retrieval_event_repository
        if repository is None or not hasattr(
            repository, "list_citations_by_turn"
        ):
            return {}
        try:
            raw = repository.list_citations_by_turn(run.conversation_id)
        except Exception:
            return {}
        labels: dict[str, frozenset[str]] = {}
        for turn_id, citations in raw.items():
            labels[str(turn_id)] = frozenset(
                str(item.get("label"))
                for item in citations
                if isinstance(item, dict) and item.get("label")
            )
        return labels

    def _memory_writes(self, run: RunRecord) -> tuple[MemoryWriteFact, ...]:
        repository = self.memory_repository
        if repository is None or not hasattr(repository, "list_memories_by_run"):
            return ()
        try:
            records = repository.list_memories_by_run(run.id)
        except Exception:
            return ()
        return tuple(
            MemoryWriteFact(
                memory_id=record.id,
                has_source=bool(record.source_entry_id),
                status=str(getattr(record.status, "value", record.status)),
            )
            for record in records
        )

    def _reflections(self, run: RunRecord) -> tuple[ReflectionFact, ...]:
        repository = self.reflection_repository
        if repository is None or not hasattr(repository, "list_records"):
            return ()
        try:
            records = repository.list_records(limit=200)
        except Exception:
            return ()
        return tuple(
            ReflectionFact(
                reflection_id=record.id,
                trigger=record.trigger,
                has_sources=bool(record.source_refs),
                insight_length=len(record.insight_content or ""),
                status=record.status,
            )
            for record in records
            if record.run_id == run.id
        )

    def evaluate_batch(
        self,
        spec: EvalHarvestSpec,
        *,
        mode: str = "deterministic",
        judge_provider: Optional[str] = None,
        judge_model: Optional[str] = None,
        persist: bool = True,
        batch_id: Optional[str] = None,
    ) -> tuple[str, Aggregation, tuple[RunEvaluation, ...]]:
        runs = harvest_runs(self._repository, spec)
        results = tuple(self.evaluate_run(run) for run in runs)
        aggregation = aggregate(results)
        if persist:
            if batch_id is None:
                batch_id = self._eval.create_batch(
                    mode=mode,
                    filters=spec.to_dict(),
                    judge_provider=judge_provider,
                    judge_model=judge_model,
                    run_count=len(results),
                )
            for result in results:
                self._eval.append_run_result(batch_id, result)
            self._eval.finish_batch(batch_id, aggregation)
        return batch_id or "", aggregation, results
