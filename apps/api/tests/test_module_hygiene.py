"""整包结构卫生检查：每个模块不允许出现"用到但未定义/未导入"的名字。

为什么要有这条测试：把 `api/app.py`（当时 8 062 行）按路由族拆成
`api/routes/*`、`api/schemas/*` 与若干 support 模块的过程中，光靠单测兜不住
这类错误——有些代码路径没有测试覆盖（例如 `isinstance(kind, MemoryKind)`
里的 `MemoryKind` 从来就没导入过，靠 `from __future__ import annotations`
让注解不求值才一直没炸），有些是"手写模块头覆盖了工具生成的导入清单"。

这条测试对**整个 `endless_task` 包**（313 个文件、约 0.5s）做静态分析。之所以从
`api/**` 扩到整包：拆引擎文件（仓储/执行/网关/工具）时同样需要它，而且扩包后
立刻抓出 6 个"注解引用了但从未导入"的类型（`ScoreCard` / `Path` / `Callable` /
`ToolOutcome` / `ProviderToolDefinition` / `TaskRecord`）——靠
`from __future__ import annotations` 一直没炸，但 `typing.get_type_hints()`、
pydantic 或 dataclass 之类一旦求值就会 ImportError。

见 docs/product-improvements/05-code-health/01-monolith-audit.md。
"""

from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

import endless_task

PACKAGE_ROOT = Path(endless_task.__file__).parent

#: 允许"未定义即使用"的内建/特殊名字。
ALLOWED = {"self", "cls", "logger", "__name__", "__file__", "__doc__"}

#: 内建名字（模块里的 __builtins__ 可能是 dict，必须显式取 builtins 模块）。
BUILTINS = set(dir(builtins))


def _check_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.alias):
            defined.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    known = defined | BUILTINS | ALLOWED
    return sorted(used - known)


class ModuleHygieneTest(unittest.TestCase):
    def test_no_undefined_names_in_package(self) -> None:
        problems: dict[str, list[str]] = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            missing = _check_module(path)
            if missing:
                problems[str(path.relative_to(PACKAGE_ROOT))] = missing
        self.assertEqual(
            problems,
            {},
            "以下模块引用了未定义/未导入的名字（搬家或重构时漏了 import）：\n"
            + "\n".join(f"  {name}: {names}" for name, names in problems.items()),
        )


if __name__ == "__main__":
    unittest.main()
