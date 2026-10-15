"""RK-2 差分测试：Rust 内核 vs Python 参考实现，**逐字相同**。

差分契约：同一语料 × 同一模式，
两边的命中集合与 meta 计数必须完全一致。

差分语料只使用契约文档化的**交集**（见 search_backend 的模块注释）：
字面量、字符类、量词、锚点、分组、或。回溯类构造已被契约显式拒绝，
由 PatternSubsetContractTest 单独守。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task._native import is_available
from endless_task._native.search import NativeSearchBackend
from endless_task.workspace_runtime.search_backend import (
    SearchOptions,
    SearchPatternError,
    python_reference_backend,
)

DIFFERENTIAL_PATTERNS = (
    "beta",
    "^beta",
    "beta$",
    "[a-z]+",
    "a|b",
    "BETA",
)


@unittest.skipUnless(is_available(), "native kernel extension not installed")
class RustVsPythonDifferentialTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        sub = cls.root / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("beta\nalpha\nbeta\n", encoding="utf-8")
        hidden = cls.root / ".git"
        hidden.mkdir()
        (hidden / "c.txt").write_text("beta\n", encoding="utf-8")
        (cls.root / "uni.txt").write_text("a\u2028beta\u2029z\n", encoding="utf-8")
        (cls.root / "bin.dat").write_bytes(b"\x00\x01beta")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_hits_and_meta_match_for_every_pattern(self) -> None:
        native = NativeSearchBackend()
        reference = python_reference_backend()
        options = SearchOptions(max_file_bytes=1000)
        for pattern in DIFFERENTIAL_PATTERNS:
            for case_sensitive in (True, False):
                with self.subTest(pattern=pattern, case_sensitive=case_sensitive):
                    got = native.search(
                        self.root, pattern, case_sensitive=case_sensitive, options=options
                    )
                    want = reference.search(
                        self.root, pattern, case_sensitive=case_sensitive, options=options
                    )
                    self.assertEqual(want.hits, got.hits)
                    self.assertEqual(want.files_scanned, got.files_scanned)
                    self.assertEqual(want.files_skipped, got.files_skipped)
                    self.assertEqual(want.truncated, got.truncated)

    def test_unsupported_pattern_is_rejected_by_name_natively(self) -> None:
        "G0-B 判据在原生路径上同样成立，且是**指名**拒绝。"
        with self.assertRaises(SearchPatternError) as caught:
            NativeSearchBackend().search(self.root, "(?<=a)b")
        self.assertEqual("unsupported_pattern_syntax", caught.exception.code)
        self.assertEqual("lookbehind", caught.exception.construct)
