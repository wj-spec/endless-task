#!/usr/bin/env python
"""删除引用「已删除会话」的 reminder / knowledge / notification 孤儿。

会话删除后，引用它的 reminder、knowledge_sources、knowledge_proposals、notifications
会变成孤儿（会话已不存在），但仍会出现在通知/知识列表里。本脚本删除这些孤儿。

安全措施：
- 运行前先对数据库做时间戳备份。
- 只删除“引用已删除会话”的行；保留其它行。
- 默认只读报告（--dry-run）打印将删除的行；加 --apply 才真正删除。

用法（在 apps/api 目录下；建议先关闭 Endless Task 应用，避免并发写）：
    uv run --cache-dir .venv/uv-cache python scripts/cleanup_orphaned_conversation_refs.py --dry-run
    uv run --cache-dir .venv/uv-cache python scripts/cleanup_orphaned_conversation_refs.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from endless_task.storage import Database

DEFAULT_DB = (
    Path.home() / "Library" / "Application Support" / "Endless Task" / "endless-task.db"
)

# 每张表的 删除SQL 与 只读SELECT（用于 dry-run 展示）。
ORPHAN = {
    "reminders": (
        "DELETE FROM reminders WHERE source_conversation_id NOT IN (SELECT id FROM conversations)",
        "SELECT * FROM reminders WHERE source_conversation_id NOT IN (SELECT id FROM conversations)",
    ),
    "knowledge_sources": (
        "DELETE FROM knowledge_sources WHERE source_conversation_id IS NOT NULL AND source_conversation_id NOT IN (SELECT id FROM conversations)",
        "SELECT * FROM knowledge_sources WHERE source_conversation_id IS NOT NULL AND source_conversation_id NOT IN (SELECT id FROM conversations)",
    ),
    "knowledge_proposals": (
        "DELETE FROM knowledge_proposals WHERE conversation_id NOT IN (SELECT id FROM conversations)",
        "SELECT * FROM knowledge_proposals WHERE conversation_id NOT IN (SELECT id FROM conversations)",
    ),
    "notifications": (
        "DELETE FROM notifications WHERE conversation_id NOT IN (SELECT id FROM conversations)",
        "SELECT * FROM notifications WHERE conversation_id NOT IN (SELECT id FROM conversations)",
    ),
}


def _row_count(con, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run 只报告）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 2

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = db_path.with_name(f"{db_path.stem}.backup-{stamp}{db_path.suffix}")
        shutil.copy2(db_path, backup)
        print(f"备份：{backup}")
        print("注意：运行前请先关闭 Endless Task 应用，避免并发写入。")

    db = Database(str(db_path))
    with db.transaction() as con:
        for table, (del_sql, sel_sql) in ORPHAN.items():
            pre = _row_count(con, table)
            if args.apply:
                cur = con.execute(del_sql)
                post = _row_count(con, table)
                print(f"{table}: {pre} -> {post}（删除 {cur.rowcount}）")
            else:
                rows = con.execute(sel_sql).fetchall()
                print(f"{table}: 孤儿 {len(rows)} 条（dry-run，不删除）")
                ref = "source_conversation_id" if "source_conversation_id" in sel_sql else "conversation_id"
                for row in rows[:10]:
                    d = dict(row)
                    title = str(d.get("title") or d.get("commitment") or d.get("kind"))[:36]
                    print(f"    id={d.get('id')} {title!r} conv={d.get(ref)} status={d.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
