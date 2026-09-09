"""S8 shell 变更对账：扫描/比对/文本读取。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from endless_task.workspace_runtime.shell_reconcile import (
    changed_paths,
    read_text_bounded,
    scan_workspace,
)


class ScanWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_skips_noisy_directories(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "b.py").write_text("b", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "c.js").write_text("c", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("d", encoding="utf-8")

        states = scan_workspace(self.root)

        self.assertIsNotNone(states)
        self.assertEqual({"a.txt", "src/b.py"}, set(states or {}))

    def test_scan_returns_none_over_budget(self) -> None:
        for index in range(5):
            (self.root / f"f{index}.txt").write_text("x", encoding="utf-8")

        self.assertIsNone(scan_workspace(self.root, max_files=3))

    def test_scan_returns_none_for_missing_root(self) -> None:
        self.assertIsNone(scan_workspace(self.root / "nope"))


class ChangedPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        states = scan_workspace(self.root)
        assert states is not None
        return states

    def test_detects_created_modified_deleted(self) -> None:
        (self.root / "keep.txt").write_text("keep", encoding="utf-8")
        (self.root / "edit.txt").write_text("before", encoding="utf-8")
        (self.root / "gone.txt").write_text("gone", encoding="utf-8")
        before = self._scan()

        (self.root / "edit.txt").write_text("after", encoding="utf-8")
        (self.root / "gone.txt").unlink()
        (self.root / "new.txt").write_text("new", encoding="utf-8")
        after = self._scan()

        changes = changed_paths(before, after)

        self.assertEqual(
            [
                ("edit.txt", "modified"),
                ("gone.txt", "deleted"),
                ("new.txt", "created"),
            ],
            [(item.relative, item.kind) for item in changes],
        )

    def test_missing_side_yields_no_changes(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        states = self._scan()
        self.assertEqual([], changed_paths(None, states))
        self.assertEqual([], changed_paths(states, None))

    def test_touch_with_same_content_still_counts_as_modified(self) -> None:
        path = self.root / "same.txt"
        path.write_text("same", encoding="utf-8")
        before = self._scan()
        os.utime(path, ns=(before["same.txt"].mtime_ns + 1_000_000,) * 2)
        after = self._scan()

        changes = changed_paths(before, after)

        self.assertEqual([("same.txt", "modified")], [(c.relative, c.kind) for c in changes])


class ReadTextBoundedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_text_and_rejects_binary_missing_and_oversize(self) -> None:
        text = self.root / "a.md"
        text.write_text("内容", encoding="utf-8")
        self.assertEqual("内容", read_text_bounded(text))

        binary = self.root / "b.bin"
        binary.write_bytes(b"\xff\xfe\x00")
        self.assertIsNone(read_text_bounded(binary))

        self.assertIsNone(read_text_bounded(self.root / "nope.md"))

        big = self.root / "big.txt"
        big.write_bytes(b"x" * 600_000)
        self.assertIsNone(read_text_bounded(big))


if __name__ == "__main__":
    unittest.main()
