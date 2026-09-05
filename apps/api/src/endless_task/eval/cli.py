from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from endless_task.runtime_v2 import RunStatus
from endless_task.storage import Database

from .aggregator import aggregate, diff
from .evaluators import DEFAULT_READ_ONLY_TOOLS, DEFAULT_WRITE_TOOLS
from .gate import (
    EvalBaseline,
    Waiver,
    gate_to_markdown,
    run_gate,
    waiver_from_string,
    write_gate_report,
)
from .harvest import EvalHarvestSpec
from .models import Aggregation
from .reporting import summary_lines, to_jsonl, to_markdown
from .service import EvaluationService
from .storage import SqliteEvalRepository
from .suites import SUITE_CATALOG


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
    if command == "gate":
        return _gate(settings, arguments)
    if command == "bundle":
        return _bundle(settings, arguments)
    if command == "suites":
        return _suites(settings, arguments)
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


def _gate(settings, arguments) -> int:
    """Release gate: candidate batch vs frozen baseline, with waivers.

    CI 门禁落盘入口（08 §OE-4/§10.3）：输出机器可读 JSON 与人类可读
    markdown（``--report-dir`` 落盘），blocking 回归时以非零退出码 fail。
    """
    database = _database(settings)
    repository = SqliteEvalRepository(database)
    suite_name = arguments.suite
    if suite_name not in SUITE_CATALOG:
        print(
            f"unknown suite {suite_name!r}; known: "
            + ", ".join(sorted(SUITE_CATALOG))
        )
        return 2
    baseline_path = Path(arguments.baseline)
    try:
        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline = EvalBaseline.from_dict(baseline_data)
    except (OSError, ValueError) as error:
        print(f"cannot load baseline {baseline_path}: {error}")
        return 2
    candidate = aggregate(repository.load_run_results(arguments.candidate))
    waivers: list[Waiver] = []
    for raw in arguments.waivers or ():
        try:
            waivers.append(waiver_from_string(raw))
        except ValueError as error:
            print(f"invalid waiver {raw!r}: {error}")
            return 2
    result = run_gate(
        suite_name=suite_name,
        baseline=baseline,
        candidate=candidate,
        waivers=waivers,
        blocking_keys=tuple(SUITE_CATALOG[suite_name].metric_keys)
        if arguments.suite_blocking
        else (),
    )
    print(gate_to_markdown(result))
    if arguments.report_dir:
        json_path, _ = write_gate_report(
            result,
            Path(arguments.report_dir),
        )
        print(f"report={json_path}")
    return 0 if result.passed else 1


def _bundle(settings, arguments) -> int:
    """Score trajectory bundles from a directory and optionally gate them.

    Closes the 09 §9 quality gap: exported bundles (OE-3/W6-3) are scored
    by the trajectory evaluator, aggregated, and optionally compared to a
    frozen baseline (reusing the release gate with suite blocking keys).
    """
    from pathlib import Path as _Path

    from endless_task.runtime_ledger.trajectory import load_trajectory_bundle
    from endless_task.eval.trajectory_eval import evaluate_trajectories

    export_root = _Path(arguments.directory or (
        settings.database_path.parent / "v2_trajectory_exports"
    ))
    if not export_root.is_dir():
        print(f"no trajectory export directory: {export_root}")
        return 2
    bundles = []
    for child in sorted(export_root.iterdir()):
        if child.is_dir() and (child / "manifest.json").exists():
            try:
                bundles.append(load_trajectory_bundle(child))
            except Exception as error:
                print(f"skip {child.name}: {error}")
    if not bundles:
        print(f"no trajectory bundles under {export_root}")
        return 2
    cards = evaluate_trajectories(bundles)
    for card in cards:
        print(
            f"bundle {card.run_id} verdict={card.verdict.value} "
            f"blockers={int(card.has_blocker)} warnings={card.warning_count}"
        )
    aggregation = aggregate(cards)
    for line in summary_lines(aggregation):
        print(line)
    if arguments.baseline:
        baseline_path = _Path(arguments.baseline)
        try:
            baseline = EvalBaseline.from_dict(
                json.loads(baseline_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError) as error:
            print(f"cannot load baseline {baseline_path}: {error}")
            return 2
        result = run_gate(
            suite_name=arguments.suite or "core_loop",
            baseline=baseline,
            candidate=aggregation,
            blocking_keys=tuple(SUITE_CATALOG[arguments.suite].metric_keys)
            if arguments.suite and arguments.suite in SUITE_CATALOG
            else (),
        )
        print(gate_to_markdown(result))
        return 0 if result.passed else 1
    return 0


def _suites(settings, arguments) -> int:
    """List registered topic suites and their metric coverage."""
    for name, suite in SUITE_CATALOG.items():
        print(
            f"{name}: {suite.description} "
            f"[{', '.join(suite.metric_keys)}]"
        )
    return 0
