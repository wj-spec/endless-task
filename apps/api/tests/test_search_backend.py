"""L3 缝层契约用例（Python 参考实现）。

这组用例不只是"测 Python 实现"，它**钉住的是契约本身** ——
Rust 内核要过的差分测试，判据就来自这里定义的行为。
所以 `test_rejects_the_same_constructs_as_the_rust_kernel` 这条
和 `crates/endless-kernel-search` 里的同类用例是**成对**的。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.workspace_runtime.search_backend import (
    SearchOptions,
    SearchPatternError,
    classify_unsupported,
    compile_pattern,
    python_reference_backend,
)

#: 与 Rust 侧 `classify_unsupported_names_each_construct` **同一张表**。
UNSUPPORTED_CASES = (
    ("(?=x)", "lookahead"),
    ("(?!x)", "negative lookahead"),
    ("(?<=x)", "lookbehind"),
    ("(?<!x)", "negative lookbehind"),
    ("(?>x)", "atomic group"),
    ("(?P=name)", "named backreference"),
    ("(a)\\1", "backreference"),
)

#: 与 Rust 侧 `classify_unsupported_allows_supported_subset` **同一张表**。
SUPPORTED_CASES = (
    "^abc$",
    "a|b",
    "[0-9]+",
    "\\d{2,4}",
    "(?:x)",
    "(?P<n>x)",
    "\\.",
)


class TempTree:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_bytes(self, relative: str, content: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def close(self) -> None:
        self._tmp.cleanup()


class PatternSubsetContractTest(unittest.TestCase):
    def test_rejects_the_same_constructs_as_the_rust_kernel(self) -> None:
        for pattern, expected in UNSUPPORTED_CASES:
            with self.subTest(pattern=pattern):
                found = classify_unsupported(pattern)
                self.assertIsNotNone(found, pattern)
                construct, _needle = found  # type: ignore[misc]
                self.assertEqual(expected, construct)

    def test_allows_the_same_supported_subset_as_the_rust_kernel(self) -> None:
        for pattern in SUPPORTED_CASES:
            with self.subTest(pattern=pattern):
                self.assertIsNone(classify_unsupported(pattern), pattern)

    def test_unsupported_is_rejected_by_name(self) -> None:
        with self.assertRaises(SearchPatternError) as caught:
            compile_pattern("(?<=a)b")
        self.assertEqual("unsupported_pattern_syntax", caught.exception.code)
        self.assertEqual("lookbehind", caught.exception.construct)
        self.assertIn("(?<=", caught.exception.needle)

    def test_invalid_pattern_is_a_separate_code(self) -> None:
        with self.assertRaises(SearchPatternError) as caught:
            compile_pattern("(unclosed")
        self.assertEqual("invalid_pattern", caught.exception.code)

    def test_case_insensitive_when_asked(self) -> None:
        self.assertIsNotNone(compile_pattern("hello", False).search("HELLO"))
        self.assertIsNone(compile_pattern("hello", True).search("HELLO"))


class ReferenceBackendContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = TempTree()
        self.backend = python_reference_backend()

    def tearDown(self) -> None:
        self.tree.close()

    def test_hits_are_relative_posix_and_sorted(self) -> None:
        self.tree.write("b.txt", "alpha\nbeta\n")
        self.tree.write("sub/a.txt", "beta\nalpha\n")
        outcome = self.backend.search(self.tree.root, "beta")

        self.assertEqual(
            (("b.txt", 2, "beta"), ("sub/a.txt", 1, "beta")),
            tuple((h.path, h.line_number, h.line_text) for h in outcome.hits),
        )
        self.assertEqual(2, outcome.files_scanned)
        self.assertFalse(outcome.truncated)

    def test_splitlines_boundaries_are_part_of_the_contract(self) -> None:
        """`\\u2028` 也是行边界 —— 写错这条差分会在"看不到换行"的差异上失败。"""
        self.tree.write("uni.txt", "a\u2028target\u2029b\n")
        outcome = self.backend.search(self.tree.root, "target")
        self.assertEqual(1, len(outcome.hits))
        self.assertEqual(2, outcome.hits[0].line_number)

    def test_skips_binary_oversized_and_ignored(self) -> None:
        self.tree.write("keep.txt", "target\n")
        self.tree.write(".git/config", "target\n")
        self.tree.write("big.txt", "x" * 64 + "\ntarget\n")
        self.tree.write_bytes("bin.dat", b"\x00\x01target")

        outcome = self.backend.search(
            self.tree.root, "target", options=SearchOptions(max_file_bytes=16)
        )
        self.assertEqual(("keep.txt",), tuple(h.path for h in outcome.hits))
        self.assertEqual(1, outcome.files_scanned)
        self.assertEqual(2, outcome.files_skipped)

    def test_caps_mark_truncated(self) -> None:
        self.tree.write("many.txt", "hit\nhit\nhit\nhit\n")
        capped = self.backend.search(
            self.tree.root, "hit", options=SearchOptions(max_results=2)
        )
        self.assertEqual(2, len(capped.hits))
        self.assertTrue(capped.truncated)

        per_file = self.backend.search(
            self.tree.root, "hit", options=SearchOptions(max_hits_per_file=1)
        )
        self.assertEqual(1, len(per_file.hits))
        self.assertTrue(per_file.truncated)