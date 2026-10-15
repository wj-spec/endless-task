"""26 号 N1③：变更 diff 的纯函数契约（`change_diff_text`）。

面板「Agent 改了什么」的判据在服务端：undo 日志的 before × 当前文件的 after
折成 unified diff（标准库 `difflib`，不手写 LCS）。本文件钉住四种形态与
「无变更 → None」的口径；路由壳由 `test_api_route_inventory` 钉路径存在。
"""

from __future__ import annotations

import unittest

from endless_task.api.serialization import change_diff_text


class ChangeDiffTextTest(unittest.TestCase):
    def test_create_shows_added_lines(self) -> None:
        diff = change_diff_text(None, "新行\n", "notes.md")

        self.assertIn("--- a/notes.md", diff)
        self.assertIn("+++ b/notes.md", diff)
        self.assertIn("+新行", diff)

    def test_overwrite_shows_both_sides(self) -> None:
        diff = change_diff_text("旧行\n", "新行\n", "notes.md")

        self.assertIn("-旧行", diff)
        self.assertIn("+新行", diff)

    def test_delete_shows_removed_lines(self) -> None:
        diff = change_diff_text("内容\n", "", "gone.md")

        self.assertIn("-内容", diff)
        self.assertNotIn("+内容", diff)

    def test_no_change_yields_none(self) -> None:
        self.assertIsNone(change_diff_text("同\n", "同\n", "same.md"))

    def test_missing_both_sides_yields_none(self) -> None:
        self.assertIsNone(change_diff_text(None, None, "ghost.md"))


if __name__ == "__main__":
    unittest.main()
