#!/usr/bin/env python
"""Delete ALL conversations and their history (project session history reset).

注意：这是彻底的破坏性操作，只保留 工作区 / 知识源 / 成果 / 模型与设置 / 迁移记录。
运行前先备份；运行前请关闭 Endless Task 应用。

用法（在 apps/api 目录）：
    uv run --cache-dir .venv/uv-cache python scripts/reset_conversation_history.py --dry-run   # 只报告
    uv run --cache-dir .venv/uv-cache python scripts/reset_conversation_history.py --apply      # 真正清空
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / "Library" / "Application Support" / "Endless Task" / "endless-task.db"

# (表名, 删除条件|None)。None=全表清空；有条件的按条件删。
# 顺序仅供参考——真正执行时会先 PRAGMA foreign_keys=OFF，从而不受外键顺序限制。
RESET_TABLES: list[tuple[str, str | None]] = [
    ("v2_tool_executions", None),
    ("v2_model_turns", None),
    ("v2_runtime_events", None),
    ("v2_context_compactions", None),
    ("v2_transcript_entries", None),
    ("v2_lane_events", None),
    ("v2_message_requests", None),
    ("v2_product_events", None),
    ("v2_conversation_pointers", None),
    ("v2_lanes", None),
    ("v2_runs", None),
    ("v2_temporary_conversations", None),
    ("v2_runtime_memories", None),
    ("v2_memory_promotions", None),
    ("tool_calls", None),
    ("turns", None),
    ("messages", None),
    ("response_variants", None),
    ("client_requests", None),
    ("command_requests", None),
    ("runtime_events", None),
    ("context_snapshots", None),
    ("conversation_summary_revisions", None),
    ("retrieval_events", None),
    ("artifact_proposals", None),
    ("memory_proposals", None),
    ("knowledge_proposals", None),
    ("task_proposals", None),
    ("uploaded_text_files", None),
    ("hub_events", None),
    ("notifications", None),
    ("reminders", "source_conversation_id IS NOT NULL"),
    ("memories", "source_conversation_id IS NOT NULL"),
    ("trace_events", None),
    ("trace_spans", None),
    ("trace_usage", None),
    ("task_runs", None),
    ("tasks", None),
    ("conversations", None),
]


def _tables(cur: sqlite3.Cursor) -> set[str]:
    rows = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--apply", action="store_true", help="真正清空（默认只报告）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不删除（默认行为）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 2

    # 用原生连接，并在事务外关闭外键约束，避免删除顺序触发外键错误。
    connection = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = OFF")
    cur = connection.cursor()

    present = _tables(cur)
    existing = [(t, w) for (t, w) in RESET_TABLES if t in present]
    missing = [t for (t, w) in RESET_TABLES if t not in present]

    if not args.apply:
        print("（dry-run，不删除）")
        for table, where in existing:
            suffix = f" WHERE {where}" if where else ""
            n = cur.execute(f"SELECT COUNT(*) FROM {table}{suffix}").fetchone()[0]
            print(f"{table}{suffix or ''}: {n} 行")
        if missing:
            print("（跳过不存在的表：", ", ".join(missing), "）")
        connection.close()
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_name(f"{db_path.stem}.reset-{stamp}{db_path.suffix}")
    shutil.copy2(db_path, backup)
    print(f"备份：{backup}")
    print("注意：运行前请先关闭 Endless Task 应用。")

    try:
        connection.execute("BEGIN IMMEDIATE")
        for table, where in existing:
            suffix = f" WHERE {where}" if where else ""
            n = cur.execute(f"DELETE FROM {table}{suffix}").rowcount
            print(f"清空 {table}{suffix or ''}: {n}")
        check = cur.execute("PRAGMA integrity_check").fetchone()[0]
        print("integrity_check:", check)
        connection.commit()
        print("完成。")
    except BaseException:
        connection.rollback()
        print("出错，事务已回滚。")
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
