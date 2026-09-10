"""模块级公开面快照：拆大文件（引擎/路由/工具）时的"对外契约不变"安全网。

为什么除了 `test_storage_method_inventory` 还要这一条：那个快照只冻结**类方法**，
而拆 `execution.py` / `gateway.py` 这类模块时，真正被人 import 的东西大量是
**模块级**的——顶层函数（`product_event_json`、`derive_approval_risk`、`_entry_json`…）、
模块级常量（`V2_CAPABILITIES`、`_SNAPSHOT_UNTRUSTED_MAX_CHARS`）以及类本身的名字。
把它们按"名字 + 种类 + 签名 + 字面量指纹"冻结之后，拆分当场就能看出：
某个顶层函数没搬过来 / 签名变了 / 常量的值被改了。

用法：
- 正常跑：断言模块面与冻结清单一致。
- 有意增删模块级名字时：`ENDLESS_TASK_UPDATE_SURFACE=1 python -m unittest tests.test_module_surface_inventory`
  重新生成，并在提交里说明。
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import unittest
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "module_surface_inventory.json"

#: 上一版快照（聚合门面的 re-export 名单靠它维持；新模块留空即可）。
_SNAPSHOT_CACHE: dict[str, dict[str, str]] = (
    json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if SNAPSHOT_PATH.exists()
    else {}
)

#: 被冻结的模块：拆到哪个就加哪个（值为冻结时的行数，纯粹给人看）。
TRACKED_MODULES = {
    "endless_task.runtime_v2.execution": 2913,
    "endless_task.runtime_v2.gateway": 2002,
    "endless_task.workspace_runtime.fs_tools": 44,
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


def _constant_fingerprint(value: object) -> str:
    if isinstance(value, (str, int, float, bool, bytes, type(None))):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_constant_fingerprint(item) for item in value) + "]"
    if isinstance(value, (set, frozenset)):
        # 集合的迭代顺序受 PYTHONHASHSEED 影响，必须排序后再指纹化，否则同一份代码
        # 在不同进程里会算出不同哈希（本测试第一次跑就踩到了）。
        return "{" + ", ".join(sorted(_constant_fingerprint(item) for item in value)) + "}"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_constant_fingerprint(k)}: {_constant_fingerprint(v)}"
            for k, v in sorted(value.items(), key=lambda kv: repr(kv[0]))
        ) + "}"
    return f"<{type(value).__name__}>"


def _defined_here(module_name: str) -> set[str]:
    """本模块源码里"亲手定义/赋值"的顶层名字（AST 口径，不看运行时）。"""
    module = importlib.import_module(module_name)
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(target := node.target, ast.Name):
            names.add(target.id)
    return names


def collect_module_surface(module_name: str) -> dict[str, str]:
    """模块级公开面：函数（含签名）、类、模块级常量（值指纹）。

    只收**本模块亲手定义**的名字（AST）或**上一版快照里已经在册**的名字：拆分后
    这些模块变成"聚合门面"，对外契约正是那些从子模块 re-export 回来的名字，必须
    继续在册；而 `from typing import Optional` 这种顺带泄漏进模块作用域的名字
    不算契约（否则快照会被几十个 `dataclass` / `Protocol` 噪音淹没）。
    """
    module = importlib.import_module(module_name)
    defined_here = _defined_here(module_name)
    known_before = set(_SNAPSHOT_CACHE.get(module_name, {}))
    surface: dict[str, str] = {}
    for name, member in vars(module).items():
        if name.startswith("__"):
            continue
        if name not in defined_here and name not in known_before:
            continue
        if inspect.isfunction(member):
            surface[name] = f"def{_signature_text(member)}"
        elif inspect.isclass(member):
            surface[name] = "class"
        elif isinstance(member, (str, int, float, bool, bytes, tuple, list, dict, set, frozenset, type(None))):
            fingerprint = _constant_fingerprint(member)
            digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
            surface[name] = f"const sha256:{digest}"
    return surface


class ModuleSurfaceInventoryTest(unittest.TestCase):
    def test_module_surface_matches_frozen_snapshot(self) -> None:
        expected = _SNAPSHOT_CACHE
        current = {
            module_name: collect_module_surface(module_name)
            for module_name in TRACKED_MODULES
        }
        if os.environ.get("ENDLESS_TASK_UPDATE_SURFACE"):
            # 重新生成时取"旧 + 新"的并集：在册的名字不会因为某次生成而掉出去。
            merged = {
                module_name: {**expected.get(module_name, {}), **members}
                for module_name, members in current.items()
            }
            SNAPSHOT_PATH.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.skipTest(
                "module surface snapshot updated: "
                + ", ".join(f"{k.split('.')[-1]}={len(v)}" for k, v in merged.items())
            )
        for module_name, members in current.items():
            short = module_name.split(".")[-1]
            with self.subTest(module=short):
                frozen = expected[module_name]
                missing = sorted(set(frozen) - set(members))
                added = sorted(set(members) - set(frozen))
                changed = sorted(
                    name
                    for name in set(frozen) & set(members)
                    if frozen[name] != members[name]
                )
                self.assertEqual(
                    (missing, added, changed),
                    ([], [], []),
                    f"{module_name} 的模块面与快照不一致：\n"
                    f"  消失: {[(n, frozen[n]) for n in missing]}\n"
                    f"  新增: {[(n, members[n]) for n in added]}\n"
                    f"  变了: {[(n, frozen[n], members[n]) for n in changed]}",
                )


if __name__ == "__main__":
    unittest.main()
