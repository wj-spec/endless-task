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
        )
        score_card = build_score_card(context)
        return RunEvaluation(
            run_id=run.id,
            run_status=run.status,
            score_card=score_card,
            replay_warnings=replay.warnings,
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
