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

"""

from __future__ import annotations

import unittest
from pathlib import Path

import endless_task

from tests.module_scope import check_file

PACKAGE_ROOT = Path(endless_task.__file__).parent


class ModuleHygieneTest(unittest.TestCase):
    def test_no_undefined_names_in_package(self) -> None:
        problems: dict[str, list[str]] = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            missing = check_file(path)
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
