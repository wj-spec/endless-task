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
    if command == "restore":
        return _restore_command(settings, arguments)

    database = Database(settings.database_path)
    database.initialize()
    destination = arguments.destination or _default_backup_path(settings.database_path)
    print(database.backup(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
