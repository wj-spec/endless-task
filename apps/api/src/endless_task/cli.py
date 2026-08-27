from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="endless-task")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="启动仅监听本机的 API 服务")
    commands.add_parser("data-path", help="显示当前 SQLite 数据库位置")
    backup = commands.add_parser("backup", help="创建经过完整性检查的 SQLite 备份")
    backup.add_argument("destination", nargs="?", type=Path)
    embeddings = commands.add_parser("embeddings", help="R5.8 向量索引管理")
    embeddings_actions = embeddings.add_subparsers(dest="embeddings_command")
    embeddings_actions.add_parser("status", help="查看嵌入配置与索引计数")
    embeddings_actions.add_parser("rebuild", help="按当前模型全量重建向量")
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

    database = Database(settings.database_path)
    database.initialize()
    destination = arguments.destination or _default_backup_path(settings.database_path)
    print(database.backup(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
