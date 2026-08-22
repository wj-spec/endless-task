from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from endless_task.api import AppSettings
from endless_task.security import configure_safe_logging
from endless_task.storage import Database


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

    database = Database(settings.database_path)
    database.initialize()
    destination = arguments.destination or _default_backup_path(settings.database_path)
    print(database.backup(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
