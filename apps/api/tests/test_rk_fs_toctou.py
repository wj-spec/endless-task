"""RK-3 对抗用例：TOCTOU（校验与使用分离）必须被**闭合**。

## 这条用例守的是什么

`resolve_workspace_path` 返回的是 **Path（字符串快照）**，不是**句柄**。
回归场景：校验通过后把中间目录
换成指向工作区外的 symlink，再按 `canonical` 读取 —— **读到区外内容，且完全不需要竞态**。

内核 `read_beneath` 用**逐段 `openat` + `O_NOFOLLOW`**，返回句柄，
把「校验」与「使用」合并成同一次系统调用解析。本文件断言那条攻击**必拒**。

## 为什么断言的是「拒绝」而不是具体错误码

`O_DIRECTORY | O_NOFOLLOW` 遇 symlink 段时，**macOS 返回 `ENOTDIR`、Linux 返回 `ELOOP`**
（本用例第一版在 macOS 上就因写死 `path_escape` 而红过）。
判据是**拒绝本身**，标签随平台。
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from endless_task._native import is_available
from endless_task._native.fs import (
    NativeFsError,
    open_beneath,
    read_beneath,
    remove_beneath,
    write_beneath,
)

SECRET = "OUTSIDE-THE-WORKSPACE"

#: 所有"被拒绝"的形态（跨平台）。
REJECTION_CODES = frozenset(
    {"path_escape", "not_a_directory", "permission_denied", "io_error"}
)


class TempWorkspace:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        self.root = self.base / "ws"
        self.outside = self.base / "outside"
        self.root.mkdir()
        self.outside.mkdir()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def swap_dir_for_symlink(self, relative: str) -> None:
        """把工作区内的目录换成指向区外的 symlink（**不需要竞态**）。"""
        real = self.root / (relative + "_real")
        os.rename(self.root / relative, real)
        os.symlink(str(self.outside), str(self.root / relative))

    def close(self) -> None:
        self._tmp.cleanup()


@unittest.skipUnless(is_available(), "native kernel extension not installed")
class WorkspaceReadToctouTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()
        self.workspace.write("sub/file.txt", "inside")
        (self.workspace.outside / "file.txt").write_text(SECRET, encoding="utf-8")

    def tearDown(self) -> None:
        self.workspace.close()

    def test_reads_normally_before_any_swap(self) -> None:
        """先证明这条路径本来是通的 —— 否则"被拒"可能是别的原因造成的。"""
        self.assertEqual(
            b"inside", read_beneath(self.workspace.root, "sub/file.txt", 1024)
        )

    def test_intermediate_symlink_swap_is_rejected(self) -> None:
        """★ 门②：探针里那条越界读取，在内核下必须失败。"""
        resolved = (self.workspace.root / "sub" / "file.txt").resolve()
        relative = resolved.relative_to(self.workspace.root.resolve()).as_posix()

        self.workspace.swap_dir_for_symlink("sub")

        with self.assertRaises(NativeFsError) as caught:
            read_beneath(self.workspace.root, relative, 1024)
        self.assertIn(caught.exception.code, REJECTION_CODES)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_leaf_symlink_is_rejected(self) -> None:
        """末段是 symlink 也拒 —— 不做"指向区内就放行"的猜测（那需要再解析一次）。"""
        os.symlink(str(self.workspace.outside / "file.txt"), str(self.workspace.root / "link.txt"))
        with self.assertRaises(NativeFsError) as caught:
            read_beneath(self.workspace.root, "link.txt", 1024)
        self.assertIn(caught.exception.code, REJECTION_CODES)

    def test_absolute_and_parent_paths_are_rejected(self) -> None:
        for bad in ("/etc/passwd", "../outside/file.txt", "sub/../../outside/file.txt"):
            with self.subTest(path=bad):
                with self.assertRaises(NativeFsError) as caught:
                    read_beneath(self.workspace.root, bad, 1024)
                self.assertEqual("invalid_path", caught.exception.code)

    def test_oversized_file_is_rejected(self) -> None:
        (self.workspace.root / "big.txt").write_text("x" * 64, encoding="utf-8")
        with self.assertRaises(NativeFsError) as caught:
            read_beneath(self.workspace.root, "big.txt", 8)
        self.assertEqual("file_too_large", caught.exception.code)

    def test_streaming_open_is_rejected_after_symlink_swap(self) -> None:
        """流式读（大文件窗口）也必须走句柄 —— C3。"""
        self.workspace.swap_dir_for_symlink("sub")

        with self.assertRaises(NativeFsError) as caught:
            open_beneath(self.workspace.root, "sub/file.txt")
        self.assertIn(caught.exception.code, REJECTION_CODES)

    def test_streaming_open_reads_normally_before_swap(self) -> None:
        """反向对照：正常时它必须是个能用的文件对象。"""
        with open_beneath(self.workspace.root, "sub/file.txt") as handle:
            self.assertEqual(["inside"], handle.read().splitlines())

    def test_write_refuses_to_escape_via_symlink(self) -> None:
        """★ 写比读更危险（读是泄漏、写是污染）：区外**绝不能**出现文件。"""
        self.workspace.swap_dir_for_symlink("sub")

        with self.assertRaises(NativeFsError) as caught:
            write_beneath(self.workspace.root, "sub/pwned.txt", b"x")
        self.assertIn(caught.exception.code, REJECTION_CODES)
        self.assertFalse(
            (self.workspace.outside / "pwned.txt").exists(),
            "区外不得被写入",
        )

    def test_remove_refuses_to_escape_via_symlink(self) -> None:
        """★ 删是毁数据：区外文件**必须还在**。"""
        victim = self.workspace.outside / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        self.workspace.swap_dir_for_symlink("sub")

        with self.assertRaises(NativeFsError) as caught:
            remove_beneath(self.workspace.root, "sub/victim.txt")
        self.assertIn(caught.exception.code, REJECTION_CODES)
        self.assertTrue(victim.exists(), "区外文件不得被删除")

    def test_write_then_read_round_trips_inside_the_root(self) -> None:
        """反向对照：正常路径必须还能用（否则"被拒"可能只是功能坏了）。"""
        write_beneath(self.workspace.root, "sub/new.txt", b"hello")
        self.assertEqual(
            b"hello", read_beneath(self.workspace.root, "sub/new.txt", 1024)
        )
        remove_beneath(self.workspace.root, "sub/new.txt")
        with self.assertRaises(NativeFsError):
            read_beneath(self.workspace.root, "sub/new.txt", 1024)
