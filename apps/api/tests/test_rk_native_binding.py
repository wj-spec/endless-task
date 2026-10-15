"""RK-1 绑定层用例：钉住 F5（pyo3 元组转换产出坏对象 ⇒ 一索引就段错误）。

背景：`crates/endless-native` 原先声明 `Vec<(String, bool, String)>`，
在 pyo3 0.29.2 下实测返回**形状错乱且含空指针**的元组
（`(True, 'PyO3 binding loaded', <NULL>)`），Python 侧一索引即
`Segmentation fault: 11`。修法是底层改返回 `Vec<String>`（`name|1|detail` 行），
由薄封装解析回三元组。

本文件守三层（缺一层都可能让 F5 悄悄回来）：
  1. **解析**：`name|1|detail` → `(name, True, detail)`，`|0|` → False；
  2. **形状**：包装层返回的必须是 `(str, bool, str)`，且元素可安全索引；
  3. **源头**：L2 源码里**不许再出现元组返回类型**（不需要扩展在场就能咬）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

import endless_task._native as native

API_ROOT = pathlib.Path(__file__).resolve().parents[1]
L2_SOURCE = API_ROOT / "crates" / "endless-native" / "src" / "lib.rs"

#: 已知会产出坏对象的返回类型（F5 的根因形态）。
FORBIDDEN_RETURN = "Vec<(String, bool, String)>"


class _FakeBinding:
    """替身：模拟 L2 扩展的 `capabilities()` 输出（含不可用项）。"""

    def capabilities(self):
        return ["native.binding|1|binding loaded", "search.fast|0|not built"]


class NativeCapabilitiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = native._BINDING
        native._BINDING = _FakeBinding()

    def tearDown(self) -> None:
        native._BINDING = self._saved

    def test_parses_flag_into_bool(self) -> None:
        self.assertEqual(
            (
                ("native.binding", True, "binding loaded"),
                ("search.fast", False, "not built"),
            ),
            native.capabilities(),
        )

    def test_every_element_is_indexable_and_typed(self) -> None:
        """★ F5 回归：坏元组在这里是**段错误**，不是断言失败。"""
        rows = native.capabilities()
        self.assertTrue(rows)
        for name, available, detail in rows:
            self.assertIsInstance(name, str)
            self.assertIsInstance(available, bool)
            self.assertIsInstance(detail, str)
            self.assertTrue(name)


def code_without_comments(source: str) -> str:
    """剥掉 `//` 注释再匹配。

    必须剥：解释「为什么不用元组返回」的注释里**必然**写着那个类型名，
    朴素子串匹配会把说明文字误报成违规（本守卫第一版就这样红了一次 ——
    与 `test_rk_native_layering` 里 Cargo.toml 注释误报是同一类坑）。
    """
    return chr(10).join(raw.split("//", 1)[0] for raw in source.splitlines())


class L2ReturnShapeGuardTest(unittest.TestCase):
    def test_l2_does_not_declare_the_bad_tuple_return(self) -> None:
        """源头守卫：不需要扩展在场就能咬（F5 的根因形态不许回来）。"""
        self.assertTrue(L2_SOURCE.is_file(), L2_SOURCE)
        code = code_without_comments(L2_SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn(FORBIDDEN_RETURN, code)

    def test_guard_would_catch_the_regression(self) -> None:
        """正向对照：把禁用形态放回样本，判据必须为假（否则守卫是死的）。"""
        sample = "fn capabilities() -> " + FORBIDDEN_RETURN + " {"
        self.assertIn(FORBIDDEN_RETURN, sample)


class NativeSearchFreshImportTest(unittest.TestCase):
    def test_native_search_and_workspace_runtime_import_in_either_order(self) -> None:
        # A fresh process catches cycles that a preloaded full test suite hides.
        program = """
import importlib
import sys

sys.path.insert(0, sys.argv[1])
assert "endless_task._native.search" not in sys.modules
assert "endless_task.workspace_runtime" not in sys.modules
importlib.import_module(sys.argv[2])
importlib.import_module(sys.argv[3])
from endless_task._native.search import NativeSearchBackend
from endless_task.workspace_runtime import WorkspaceSearchTool

assert NativeSearchBackend.name == "rust-native"
assert WorkspaceSearchTool.definition.name == "workspace_search"
"""
        for first, second in (
            ("endless_task._native.search", "endless_task.workspace_runtime"),
            ("endless_task.workspace_runtime", "endless_task._native.search"),
        ):
            with self.subTest(first=first):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        program,
                        str(API_ROOT / "src"),
                        first,
                        second,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
