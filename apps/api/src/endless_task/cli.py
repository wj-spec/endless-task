from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from typing import Optional, Sequence

from endless_task.api import AppSettings
from endless_task.security import configure_safe_logging
from endless_task.storage import Database


def _embeddings_command(settings, arguments) -> int:
    from endless_task.knowledge import EmbeddingIndexer, build_embedder
    from endless_task.storage import SqliteKnowledgeRepository

    action = arguments.embeddings_command or "status"
    database = Database(settings.database_path)
    database.initialize()
    if not settings.embedding_enabled:
        print("嵌入未启用：设置 ENDLESS_TASK_EMBEDDING=1 后重试。")
        return 1 if action == "rebuild" else 0
    embedder = build_embedder(
        backend=settings.embedding_backend,
        embedding_model=settings.embedding_model,
        provider_name=settings.provider_name,
        api_key=settings.api_key,
        base_url=settings.base_url,
        cache_dir=settings.database_path.parent,
        local_repo=settings.embedding_local_repo,
        local_url_base=settings.embedding_local_url_base,
    )
    if embedder is None:
        print("嵌入后端配置不完整，已退回纯字面检索。")
        return 1
    indexer = EmbeddingIndexer(
        database,
        embedder,
        SqliteKnowledgeRepository(database),
        max_chars=settings.embedding_max_chars,
        batch_size=settings.embedding_batch_size,
    )
    if action == "status":
        info = indexer.stats()
        print(f"backend={settings.embedding_backend}")
        print(f"model={embedder.model_name}")
        print(f"counts={info['counts']}")
        print(f"models={info['models']}")
        return 0
    stats = indexer.rebuild()
    for scope, total in stats.items():
        print(f"{scope}: {total} vectors")
    return 0


def _default_backup_path(database_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return database_path.parent / "backups" / f"endless-task-{timestamp}.db"


def _trajectory_export_command(settings, arguments) -> int:
    """显式导出 run 的 trajectory bundle（08 §OE-3 显式请求路径）。

    ``RunTrajectoryExporter.export_run`` 对任意终态 run 写
    ``v2_trajectory_exports/trajectory-<run-id>/`` 目录；失败 run 在
    trace mode on 时已自动导出（W6-3），本命令提供按需入口。
    """
    from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
    from endless_task.runtime_v2.run_trajectory import (
        RunTrajectoryExporter,
        default_journal_reader,
    )
    from endless_task.storage import SqliteRuntimeV2Repository

    database = Database(settings.database_path)
    database.initialize()
    repository = SqliteRuntimeV2Repository(database)
    ledger = SqliteRuntimeLedger(database)
    exporter = RunTrajectoryExporter(
        export_root=settings.database_path.parent / "v2_trajectory_exports",
        journal_reader=default_journal_reader(repository),
        runtime_version=_trace_runtime_version(),
        provider=settings.provider_name,
        model=settings.model,
        config_fingerprint=settings.system_prompt_version,
        usage_ledger=ledger,
    )
    directory = exporter.export_run(arguments.run_id)
    if directory is None:
        print(f"failed to export run {arguments.run_id}")
        return 1
    print(directory)
    return 0


def _flags_command(settings, arguments) -> int:
    """Agent Platform feature-flag 状态与回滚演练（09 §6/§9 运维）。"""
    from endless_task.api.agent_flags import (
        agent_platform_flags,
        rollback_dry_run,
    )

    if arguments.rollback_dry_run:
        result = rollback_dry_run(settings, arguments.rollback_dry_run)
        print(
            f"flag={result['flag']} wired={str(result['wired']).lower()} "
            f"container_built={str(result.get('containerBuilt', False)).lower()}"
        )
        if result.get("field") is not None:
            print(
                f"field={result['field']} before={result['before']} "
                f"after={result['after']}"
            )
        print(f"note={result['note']}")
        return 0 if result.get("containerBuilt", False) or not result["wired"] else 1

    for flag in agent_platform_flags(settings):
        value = flag.current_value if flag.wired else "(未接线)"
        print(
            f"{flag.env_name}={value}"
            + (f" [field={flag.field}, rollback={flag.rollback_value}]" if flag.wired else "")
        )
    return 0


def _retention_command(settings, arguments) -> int:
    """一键 trace 清理与 trajectory 导出目录整理（08 §11 一键清理）。

    dry-run 默认：只报告将删除的行/目录，不实际删除（09 §9 运维安全）。
    """
    from endless_task.runtime_ledger.retention import (
        cleanup_trajectory_exports,
        sweep_trace_ledger,
    )

    database = Database(settings.database_path)
    database.initialize()
    report = sweep_trace_ledger(
        database,
        retention_days=arguments.days,
        audit_retention_days=arguments.audit_days,
        apply=arguments.apply,
        include_safety_critical=arguments.include_safety_critical,
    )
    export_report = cleanup_trajectory_exports(
        settings.database_path.parent / "v2_trajectory_exports",
        keep_latest=arguments.keep_bundles,
        apply=arguments.apply,
    )
    print(
        f"dry_run={str(report.dry_run).lower()} "
        f"spans={report.spans_removed} events={report.events_removed} "
        f"usage={report.usage_removed} "
        f"safety_critical_kept={report.safety_critical_kept} "
        f"trajectory_bundles={export_report.trajectory_bundles_removed}"
    )
    return 0


def _trace_runtime_version() -> str:
    from endless_task import __version__

    return __version__ or "0.0.0"


def _print_runtime_v2_report(report) -> None:
    print(f"already_migrated={str(report.already_migrated).lower()}")
    print(f"conversations={report.conversation_count}")
    print(f"lanes={report.lane_count}")
    print(f"entries={report.entry_count}")
    print(f"runs={report.run_count}")
    print(f"model_turns={report.model_turn_count}")
    print(f"tool_executions={report.tool_execution_count}")
    print(f"runtime_events={report.runtime_event_count}")
    print(f"context_compactions={report.context_compaction_count}")
    print(f"conversation_trees={report.conversation_tree_count}")
    print(f"branch_conversations={report.branch_conversation_count}")
    print(f"conversation_mappings={report.conversation_mapping_count}")
    print(
        "legacy_temporary_lane_repairs="
        f"{report.legacy_temporary_lane_repair_count}"
    )
    if report.warnings:
        print("warnings:")
        for warning in report.warnings:
            print(f"- {warning}")


def _print_runtime_v2_audit_report(report) -> None:
    print(f"passed={str(report.passed).lower()}")
    print(f"migration_state={report.migration_state}")
    print(f"conversations={report.conversation_count}")
    print(f"conversation_mappings={report.mapped_conversation_count}")
    print(f"conversation_trees={report.conversation_tree_count}")
    print(f"branch_conversations={report.branch_conversation_count}")
    print(f"pending_migration_conversations={report.pending_migration_count}")
    print(f"rollback_reconciliation_trees={report.rollback_reconciliation_count}")
    print(
        "legacy_temporary_lane_repairs="
        f"{report.legacy_temporary_lane_repair_count}"
    )
    print(
        "pending_legacy_temporary_lane_repairs="
        f"{report.pending_legacy_temporary_lane_repair_count}"
    )
    if report.errors:
        print("errors:")
        for error in report.errors:
            print(f"- {error}")


def _runtime_v2_migration_command(settings, arguments) -> int:
    from endless_task.runtime_v2 import RuntimeV2MigrationService

    if not arguments.dry_run and not arguments.apply and not arguments.audit:
        print("请指定 --dry-run、--apply 或 --audit。")
        return 2

    database = Database(settings.database_path)
    if not database.path.is_file():
        mode = (
            "audit"
            if arguments.audit
            else "dry-run"
            if arguments.dry_run
            else "apply"
        )
        print(f"mode={mode}")
        print("source_unchanged=true")
        print(f"error=database does not exist: {database.path}")
        return 1

    if arguments.audit:
        with tempfile.TemporaryDirectory(prefix="endless-task-audit-") as directory:
            temporary_path = Path(directory) / "audit.db"
            database.backup(temporary_path)
            temporary_database = Database(temporary_path)
            temporary_database.initialize()
            report = RuntimeV2MigrationService(temporary_database).audit()
        print("mode=audit")
        print("source_unchanged=true")
        _print_runtime_v2_audit_report(report)
        return 0 if report.passed else 1

    if arguments.dry_run:
        with tempfile.TemporaryDirectory(prefix="endless-task-migration-") as directory:
            temporary_path = Path(directory) / "dry-run.db"
            database.backup(temporary_path)
            temporary_database = Database(temporary_path)
            temporary_database.initialize()
            report = RuntimeV2MigrationService(temporary_database).migrate()
        print("mode=dry-run")
        print("source_unchanged=true")
        _print_runtime_v2_report(report)
        return 0

    backup_path = _default_backup_path(settings.database_path)
    created_backup = database.backup(backup_path)
    print("mode=apply")
    print(f"backup={created_backup}")
    try:
        database.initialize()
        report = RuntimeV2MigrationService(database).migrate()
    except Exception as error:
        print("migrated=false")
        print(f"error={error}")
        return 1
    print("migrated=true")
    _print_runtime_v2_report(report)
    return 0


def _restore_command(settings, arguments) -> int:
    if not arguments.confirm:
        print("恢复会替换当前数据库；请停止应用并显式传入 --confirm。")
        return 2

    database = Database(settings.database_path)
    source = arguments.source.expanduser().resolve()
    safety_backup: Optional[Path] = None
    try:
        if database.path.exists():
            safety_backup = database.backup(_default_backup_path(database.path))
        restored = database.restore(source)
    except (FileNotFoundError, FileExistsError, ValueError, sqlite3.DatabaseError) as error:
        print("mode=restore")
        if safety_backup is not None:
            print(f"safety_backup={safety_backup}")
        print("restored=false")
        print(f"error={error}")
        return 1

    print("mode=restore")
    print(f"source={source}")
    if safety_backup is not None:
        print(f"safety_backup={safety_backup}")
    print(f"target={restored}")
    print("integrity_check=ok")
    print("restored=true")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="endless-task")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="启动仅监听本机的 API 服务")
    commands.add_parser("data-path", help="显示当前 SQLite 数据库位置")
    backup = commands.add_parser("backup", help="创建经过完整性检查的 SQLite 备份")
    backup.add_argument("destination", nargs="?", type=Path)
    restore = commands.add_parser("restore", help="从 SQLite 备份恢复当前数据库")
    restore.add_argument("source", type=Path)
    restore.add_argument(
        "--confirm",
        action="store_true",
        help="确认应用已停止，并允许替换当前数据库",
    )
    trajectory_export = commands.add_parser(
        "trajectory-export",
        help="显式导出 run 的 trajectory bundle 到 v2_trajectory_exports/（08 §OE-3）",
    )
    trajectory_export.add_argument("run_id", help="v2 run id")
    flags = commands.add_parser(
        "flags", help="Agent Platform feature-flag 状态（09 §6）；--rollback-dry-run 演练回滚"
    )
    flags.add_argument(
        "--rollback-dry-run",
        metavar="FLAG",
        default=None,
        help="演练：把指定 flag 置关闭并验证容器可构建",
    )
    retention = commands.add_parser(
        "retention",
        help="trace/usage 与 trajectory 导出的一键清理（08 §11；dry-run 默认）",
    )
    retention.add_argument(
        "--days", type=int, default=30, help="trace 保留天数（默认 30）"
    )
    retention.add_argument(
        "--audit-days", type=int, default=90, help="audit/safety 事件保留天数（默认 90）"
    )
    retention.add_argument(
        "--keep-bundles", type=int, default=10, help="保留最近 N 个 trajectory bundle"
    )
    retention.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run）")
    retention.add_argument(
        "--include-safety-critical",
        action="store_true",
        help="同时清理超过 audit 窗口的 safety-critical 事件",
    )
    embeddings = commands.add_parser("embeddings", help="R5.8 向量索引管理")
    embeddings_actions = embeddings.add_subparsers(dest="embeddings_command")
    embeddings_actions.add_parser("status", help="查看嵌入配置与索引计数")
    embeddings_actions.add_parser("rebuild", help="按当前模型全量重建向量")
    migration = commands.add_parser(
        "migrate-runtime-v2",
        help="迁移 v1 聊天与 Runtime 数据到 v2 Agent Runtime",
    )
    migration_modes = migration.add_mutually_exclusive_group()
    migration_modes.add_argument(
        "--dry-run",
        action="store_true",
        help="在临时数据库副本上预演迁移，不修改当前数据库",
    )
    migration_modes.add_argument(
        "--apply",
        action="store_true",
        help="先创建备份，再执行迁移",
    )
    migration_modes.add_argument(
        "--audit",
        action="store_true",
        help="只读校验迁移状态、数量与关系",
    )
    eval_command = commands.add_parser(
        "eval",
        help="离线自动化评估（质量/回归）：对录制的 v2 Run 批量打分",
    )
    eval_sub = eval_command.add_subparsers(dest="eval_command")
    eval_run = eval_sub.add_parser("run", help="筛选 Run 并跑确定性评估（可持久化为批次）")
    eval_run.add_argument("--conversation", default=None)
    eval_run.add_argument(
        "--status",
        action="append",
        dest="statuses",
        default=None,
        help="可重复；默认 completed",
    )
    eval_run.add_argument("--require-tools", action="store_true")
    eval_run.add_argument("--max", type=int, default=None, dest="max_runs")
    eval_run.add_argument("--mode", default="deterministic")
    eval_run.add_argument("--judge-provider", default=None)
    eval_run.add_argument("--judge-model", default=None)
    eval_run.add_argument(
        "--read-only-tool", action="append", default=[], dest="read_only_tools"
    )
    eval_run.add_argument(
        "--write-tool", action="append", default=[], dest="write_tools"
    )
    eval_run.add_argument("--no-persist", action="store_true")
    eval_report = eval_sub.add_parser("report", help="输出某批次的报告")
    eval_report.add_argument("--batch", required=True)
    eval_export = eval_sub.add_parser("export", help="导出某批次为 jsonl 或 markdown")
    eval_export.add_argument("--batch", required=True)
    eval_export.add_argument(
        "--format", choices=["jsonl", "markdown"], default="jsonl"
    )
    eval_diff = eval_sub.add_parser(
        "diff", help="对比两个批次，检出回归（CI 门禁语义）"
    )
    eval_diff.add_argument("--baseline", required=True)
    eval_diff.add_argument("--candidate", required=True)
    eval_diff.add_argument(
        "--tolerance",
        action="append",
        default=[],
        dest="tolerances",
        help="形如 metric=0.05；可重复",
    )
    eval_gate = eval_sub.add_parser(
        "gate",
        help="release gate：候选批次 vs 冻结 baseline JSON artifact；"
        "支持 waiver；blocking 回归时非零退出（CI 门禁）",
    )
    eval_gate.add_argument("--suite", required=True, help="SUITE_CATALOG 中的 suite 名")
    eval_gate.add_argument(
        "--baseline",
        required=True,
        help="冻结 baseline JSON 文件（EvalBaseline.to_dict 产物）",
    )
    eval_gate.add_argument("--candidate", required=True, help="候选 eval batch id")
    eval_gate.add_argument(
        "--waiver",
        action="append",
        default=[],
        dest="waivers",
        help="形如 metric_key;reason;owner;YYYY-MM-DD；可重复",
    )
    eval_gate.add_argument(
        "--report-dir",
        default=None,
        help="落盘 gate-<suite>.json / gate-<suite>.md（CI artifact）",
    )
    eval_gate.add_argument(
        "--suite-blocking",
        action="store_true",
        help="blocking keys 取 suite 的 metric_keys 而非聚合默认集",
    )
    eval_bundle = eval_sub.add_parser(
        "bundle",
        help="对 v2_trajectory_exports/ 下的 bundle 打分聚合（可选对 baseline gate）",
    )
    eval_bundle.add_argument(
        "--directory",
        default=None,
        help="trajectory bundle 目录（默认 <db>.parent/v2_trajectory_exports）",
    )
    eval_bundle.add_argument(
        "--baseline",
        default=None,
        help="冻结 baseline JSON（有则对聚合跑 release gate）",
    )
    eval_bundle.add_argument(
        "--suite",
        default=None,
        help="gate 使用的 suite 名（--baseline 时有效）",
    )
    eval_bundle.add_argument(
        "--budget",
        default=None,
        help="批准预算 JSON（metric -> 上限；超出即 fail，08 §10.3）",
    )
    eval_suites = eval_sub.add_parser(
        "suites", help="列出已注册的专题 suite 与 metric 覆盖"
    )
    eval_sub.add_parser("batches", help="列出已知评估批次")
    arguments = parser.parse_args(argv)

    settings = AppSettings.from_environment()
    configure_safe_logging(api_key=settings.api_key)
    command = arguments.command or "serve"
    if command == "serve":
        from endless_task.api.main import run

        run()
        return 0
    if command == "data-path":
        print(settings.database_path)
        return 0

    if command == "embeddings":
        return _embeddings_command(settings, arguments)

    if command == "migrate-runtime-v2":
        return _runtime_v2_migration_command(settings, arguments)
    if command == "eval":
        from endless_task.eval.cli import run_eval_command

        return run_eval_command(settings, arguments)
    if command == "trajectory-export":
        return _trajectory_export_command(settings, arguments)
    if command == "retention":
        return _retention_command(settings, arguments)
    if command == "flags":
        return _flags_command(settings, arguments)
    if command == "restore":
        return _restore_command(settings, arguments)

    database = Database(settings.database_path)
    database.initialize()
    destination = arguments.destination or _default_backup_path(settings.database_path)
    print(database.backup(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
