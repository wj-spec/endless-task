from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from endless_task.runtime_v2.domain import RunRecord, RunStatus

if TYPE_CHECKING:
    from endless_task.storage.sqlite_runtime_v2_repository import (
        SqliteRuntimeV2Repository,
    )


@dataclass(frozen=True)
class EvalHarvestSpec:
    """Describes which persisted runs form an evaluation dataset."""

    conversation_id: Optional[str] = None
    statuses: Sequence[RunStatus] = (RunStatus.COMPLETED,)
    require_tools: bool = False
    max_runs: Optional[int] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "statuses": [status.value for status in self.statuses],
            "require_tools": self.require_tools,
            "max_runs": self.max_runs,
        }


def harvest_runs(
    repository: "SqliteRuntimeV2Repository",
    spec: EvalHarvestSpec,
) -> tuple[RunRecord, ...]:
    """Select candidate runs from the v2 repository, source-purely (read-only)."""
    candidates = list(
        repository.list_runs(
            conversation_id=spec.conversation_id,
            statuses=tuple(spec.statuses),
        )
    )
    if spec.require_tools:
        candidates = [run for run in candidates if _run_has_tools(repository, run)]
    if spec.max_runs is not None:
        candidates = candidates[: spec.max_runs]
    return tuple(candidates)


def _run_has_tools(repository: "SqliteRuntimeV2Repository", run: RunRecord) -> bool:
    for turn in repository.list_model_turns(run.id):
        if repository.list_tool_executions(turn.id):
            return True
    return False
