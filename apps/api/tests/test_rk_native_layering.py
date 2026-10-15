"""RK-1 守卫：L1 内核与 L2 适配层保持单向依赖。

两条铁律：
  A. `crates/` 下除 `endless-native` 外，**任何 crate 不许依赖 pyo3**
     （L1 内核不知道 Python 存在，必须能 cargo test 独立全绿）；
  B. `src/endless_task/**` 里**只有 `_native/` 允许 import Rust 扩展**。

为什么必须有「用例内正向对照」：批次 G 的教训 —— 守卫很容易变成
「钉住了扫描器、没钉住针」。所以本文件除断言零违规外，还必须断言
**扫描器在合成样本上会红**；否则扫描器被改坏时用例会静默全绿。
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
CRATES_ROOT = API_ROOT / "crates"
SOURCE_ROOT = API_ROOT / "src" / "endless_task"

NATIVE_CRATE = "endless-native"
NATIVE_PACKAGE = SOURCE_ROOT / "_native"
FORBIDDEN_IMPORTS = ("_native_core",)

def declares_pyo3(manifest_text: str) -> bool:
    """Cargo.toml 是否**声明**了 pyo3 依赖。

    只看依赖行：注释里的字样（例如「本 crate 零 pyo3」）不算。
    这条是被正向对照抓出来的 —— 朴素子串匹配会把说明文字误报成违规。
    """
    for raw in manifest_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[dependencies.pyo3]") or line.startswith("[dependencies.pyo3."):
            return True
        if not line or line.startswith("["):
            continue
        if line.startswith("pyo3") and ("=" in line or "." in line):
            return True
    return False


def crates_depending_on_pyo3(crates_root: Path) -> list[str]:
    """返回除 L2 适配层外、**仍**声明了 pyo3 依赖的 crate 名。"""
    offenders: list[str] = []
    for manifest in sorted(crates_root.glob("*/Cargo.toml")):
        if manifest.parent.name == NATIVE_CRATE:
            continue
        if declares_pyo3(manifest.read_text(encoding="utf-8")):
            offenders.append(manifest.parent.name)
    return offenders

def python_files_importing_native(source_root: Path) -> list[str]:
    """返回 NATIVE_PACKAGE 之外、仍 import Rust 扩展的 .py 相对路径。"""
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if NATIVE_PACKAGE in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            else:
                continue
            if any(f in n for n in names for f in FORBIDDEN_IMPORTS):
                offenders.append(str(path.relative_to(source_root)))
    return offenders

class RustKernelLayeringGuardTest(unittest.TestCase):
    def test_crates_root_exists(self) -> None:
        """防空集合假绿：扫描根必须存在且真有 crate。"""
        self.assertTrue(CRATES_ROOT.is_dir(), CRATES_ROOT)
        self.assertGreaterEqual(len(list(CRATES_ROOT.glob("*/Cargo.toml"))), 2)

    def test_only_native_crate_may_depend_on_pyo3(self) -> None:
        self.assertEqual([], crates_depending_on_pyo3(CRATES_ROOT))

    def test_only_native_package_may_import_the_extension(self) -> None:
        self.assertEqual([], python_files_importing_native(SOURCE_ROOT))

    def test_scanners_go_red_on_synthetic_violations(self) -> None:
        """正向对照：两个扫描器都必须能在合成样本上变红。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crate = root / "fake-crate"
            crate.mkdir()
            (crate / "Cargo.toml").write_text('pyo3 = "0.29"\n', encoding="utf-8")
            self.assertEqual(["fake-crate"], crates_depending_on_pyo3(root))

            (root / "bad.py").write_text(
                "from endless_task import _native_core\n", encoding="utf-8"
            )
            self.assertEqual(["bad.py"], python_files_importing_native(root))
