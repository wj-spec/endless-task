from __future__ import annotations

from typing import Optional

from endless_task.runtime_v2 import RunStatus
from endless_task.storage import Database

from .aggregator import aggregate, diff
from .evaluators import DEFAULT_READ_ONLY_TOOLS, DEFAULT_WRITE_TOOLS
from .harvest import EvalHarvestSpec
from .models import Aggregation
from .reporting import summary_lines, to_jsonl, to_markdown
from .service import EvaluationService
from .storage import SqliteEvalRepository


def run_eval_command(settings, arguments) -> int:
    command = getattr(arguments, "eval_command", None) or "batches"
    if command == "run":
        return _run(settings, arguments)
    if command == "report":
        return _report(settings, arguments)
    if command == "export":
        return _export(settings, arguments)
    if command == "diff":
        return _diff(settings, arguments)
    if command == "batches":
        return _batches(settings, arguments)
    return 2


def _database(settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _run(settings, arguments) -> int:
    statuses = tuple(
        RunStatus(value) for value in (arguments.statuses or ["completed"])
    )
    spec = EvalHarvestSpec(
        conversation_id=arguments.conversation,
        statuses=statuses,
        require_tools=bool(arguments.require_tools),
        max_runs=arguments.max_runs,
    )
    database = _database(settings)
    service = EvaluationService(
        database,
        read_only_tools=frozenset(DEFAULT_READ_ONLY_TOOLS) | frozenset(
            arguments.read_only_tools or ()
        ),
        write_tools=frozenset(DEFAULT_WRITE_TOOLS) | frozenset(
            arguments.write_tools or ()
        ),
    )
    batch_id, aggregation, _results = service.evaluate_batch(
        spec,
        mode=arguments.mode,
        judge_provider=arguments.judge_provider,
        judge_model=arguments.judge_model,
        persist=not arguments.no_persist,
    )
    for line in summary_lines(aggregation):
        print(line)
    if arguments.no_persist:
        print("persisted=false")
    else:
        print(f"batch={batch_id}")
    return 0


def _report(settings, arguments) -> int:
    database = _database(settings)
    repository = SqliteEvalRepository(database)
    results = repository.load_run_results(arguments.batch)
    aggregation = aggregate(results)
    print(to_markdown(results, aggregation))
    for line in summary_lines(aggregation):
        print(line)
    return 0


def _export(settings, arguments) -> int:
    database = _database(settings)
    repository = SqliteEvalRepository(database)
    results = repository.load_run_results(arguments.batch)
    if arguments.format == "markdown":
        aggregation = aggregate(results)
        print(to_markdown(results, aggregation))
    else:
        print(to_jsonl(results))
    return 0


def _tolerances(raw_entries: list[str]) -> dict[str, float]:
    tolerances: dict[str, float] = {}
    for entry in raw_entries:
        key, _, value = entry.partition("=")
        tolerances[key.strip()] = float(value)
    return tolerances


def _diff(settings, arguments) -> int:
    database = _database(settings)
    repository = SqliteEvalRepository(database)
    baseline = aggregate(repository.load_run_results(arguments.baseline))
    candidate = aggregate(repository.load_run_results(arguments.candidate))
    result = diff(
        baseline,
        candidate,
        tolerances=_tolerances(arguments.tolerances or []),
    )
    print(
        f"pass_rate baseline={result.baseline_total_pass_rate} "
        f"candidate={result.candidate_total_pass_rate}"
    )
    for delta in result.deltas:
        arrow = " <- regression" if delta.regressed else ""
        print(
            f"{delta.key}: {delta.baseline_value:.4f} -> {delta.candidate_value:.4f} "
            f"(delta={delta.delta:+.4f}){arrow}"
        )
    print(f"blocking_regressions={len(result.blocking_regressions)}")
    return result.exit_code


def _batches(settings, arguments) -> int:
    database = _database(settings)
    repository = SqliteEvalRepository(database)
    for batch in repository.list_batches():
        print(
            f"{batch.id} mode={batch.mode} status={batch.status} "
            f"runs={batch.run_count} created={batch.created_at}"
        )
    return 0
