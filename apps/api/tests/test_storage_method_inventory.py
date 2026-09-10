"""公开方法清单快照：拆大文件（仓储/引擎）时的"对外契约不变"安全网。

为什么要它：`SqliteRuntimeV2Repository` 有 80+ 个方法、3 500 多行，要按表族拆成
mixin。拆分是纯机械操作（方法搬家、类不变），但风险在于"某个方法没了/改名了/
参数变了"——那会在运行时以 AttributeError/TypeError 的形式随机爆在某个调用点。
方法集合 + 签名快照能在拆分当场显形。

用法：
- 正常跑：断言方法集合与签名与冻结清单一致。
- 有意增删方法时：`ENDLESS_TASK_UPDATE_SURFACE=1 python -m unittest tests.test_storage_method_inventory`
  重新生成，并在提交里说明。
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

from endless_task.storage import Database
from endless_task.storage.sqlite_runtime_v2_repository import (
    SqliteRuntimeV2Repository,
)

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "storage_method_inventory.json"

#: 被冻结的类：拆到哪个就加哪个。
TRACKED = {
    "SqliteRuntimeV2Repository": SqliteRuntimeV2Repository,
}


def _signature_text(function) -> str:
    signature = inspect.signature(function)
    parts = []
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        text = name
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            text = f"*{name}"
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            text = f"**{name}"
        if parameter.default is not inspect.Parameter.empty:
            text += "=..."
        parts.append(text)
    return f"({', '.join(parts)})"


def collect_surface(klass: type) -> list[str]:
    entries = set()
    for name, member in inspect.getmembers(klass):
        if name.startswith("__") and name != "__init__":
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        entries.add(f"{name}{_signature_text(member)}")
    return sorted(entries)


class StorageMethodInventoryTest(unittest.TestCase):
    def test_method_surface_matches_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "surface.db")
            database.initialize()
            del database  # 只为确保模块可实例化环境可用
        current = {
            name: collect_surface(klass) for name, klass in TRACKED.items()
        }
        if os.environ.get("ENDLESS_TASK_UPDATE_SURFACE"):
            SNAPSHOT_PATH.write_text(
                json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.skipTest(
                "surface snapshot updated: "
                + ", ".join(f"{k}={len(v)}" for k, v in current.items())
            )
        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        for name, members in current.items():
            with self.subTest(klass=name):
                missing = sorted(set(expected[name]) - set(members))
                added = sorted(set(members) - set(expected[name]))
                self.assertEqual(
                    (missing, added),
                    ([], []),
                    f"{name} 的公开方法面与快照不一致：\n  消失: {missing}\n  新增: {added}",
                )


if __name__ == "__main__":
    unittest.main()
