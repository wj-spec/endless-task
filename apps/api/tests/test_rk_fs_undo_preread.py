"""RK-3 C6：undo/diff 的「前置读」必须走句柄 —— 区外内容绝不进撤销日志。

## 这条用例守的是什么

四个 fs 工具（write / edit / delete / manage_paths）在改动前会读一次「旧内容」，
喂撤销日志与 `before_hash`。这些前置读**曾经按路径**（`Path.read_text`/`read_bytes`），
即「校验通过之后，再拿字符串快照去读」——校验后把叶子换成指向区外的 symlink，
前置读就会**跟随链接读到区外内容**，全程不需要竞态。

最重的一条在 delete：内核 `remove_beneath` 的逐段 `O_NOFOLLOW` 停在**父目录**，
末段 `unlinkat` 删掉的是 symlink 本身且**成功**（`crates/endless-kernel-fs` 的实现）⇒
按路径的旧前置读会把区外内容记进撤销日志、把区外内容的哈希记进 mutation 账，
而操作看起来一切正常。本文件断言：前置读换 symlink **即拒**，日志零泄漏。

## 判别性（谁在承重）

- `test_delete_leaf_swap_...` 是**行为锚点**：按路径前置读 ⇒ 三重红（不拒、日志含区外内容、链接消失）；
  快照被软化成「symlink → None」⇒ 第一重红（操作不再被拒）。
- 正向对照用例证明「正常删除确实记日志」——否则「日志为空」可以空转。
- `NoPathPreReadScanTest` 静态守卫覆盖其余调用点（write/manage 的后续内核写本来就会拒，
  行为上分不出新旧），扫描器带**用例内正向对照**（判据被弄坏时用例自己红）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteUndoJournalRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs.common import kernel_read_undo_snapshot
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.path_safety import resolve_workspace_path
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.undo_service import WorkspaceUndoService

SECRET = "OUTSIDE-THE-WORKSPACE"


def _call(conversation_id: str, **arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="tool",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-10-08T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


class _FsToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "preread.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.root = Path(self._temp.name) / "ws"
        self.root.mkdir()
        self.workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(self.workspace.id, str(self.root))
        self.conversation = self.chat.create_or_reuse_empty_conversation(
            self.workspace.id
        )
        self.effect_log = EffectLog(Path(self._temp.name) / "logs")
        self.undo_repository = SqliteUndoJournalRepository(self.database)
        self.undo = WorkspaceUndoService(repository=self.undo_repository)
        self.write = WriteWorkspaceFileTool(
            self.resolver, self.effect_log, undo_service=self.undo
        )
        self.delete = DeleteWorkspaceFileTool(
            self.resolver, self.effect_log, undo_service=self.undo
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _outside_secret(self) -> Path:
        secret = Path(self._temp.name) / "outside_secret.txt"
        secret.write_text(SECRET, encoding="utf-8")
        return secret

    def _journal(self):
        return self.undo_repository.list_for_conversation(self.conversation.id)


class DeleteLeafSwapTest(_FsToolCase):
    """行为锚点：delete 的叶子换链 —— 旧按路径前置读会泄漏的那条路。"""

    def test_delete_rejects_leaf_swap_and_journal_never_sees_outside_content(
        self,
    ) -> None:
        target = self._write("data.txt", "inside")
        secret = self._outside_secret()

        import endless_task.workspace_runtime.fs.delete_workspace_file as delete_mod

        real_resolve = delete_mod.resolve_workspace_path

        def swap_then_resolve(root, path):
            # resolve 通过时还是真文件；返回前把叶子换成指向区外的 symlink ——
            # 这正是「校验与使用分离」的攻击面，不需要竞态。
            resolved = real_resolve(root, path)
            target.unlink()
            target.symlink_to(secret)
            return resolved

        with mock.patch.object(
            delete_mod, "resolve_workspace_path", swap_then_resolve
        ):
            with self.assertRaises(ToolError):
                asyncio.run(
                    self.delete.execute(
                        _call(self.conversation.id, path="data.txt"), _token()
                    )
                )

        # 三重判据：区外内容绝不进撤销日志；操作整体被拒（不是"删掉链接算成功"）；
        # 区外文件与现场原封不动。
        self.assertEqual([], list(self._journal()))
        self.assertEqual(SECRET, secret.read_text(encoding="utf-8"))
        self.assertTrue(target.is_symlink())

    def test_delete_records_before_content_normally(self) -> None:
        """正向对照：正常删除确实记日志 —— 否则上一条的"日志为空"可以空转。"""
        self._write("data.txt", "inside")

        asyncio.run(
            self.delete.execute(
                _call(self.conversation.id, path="data.txt"), _token()
            )
        )

        journal = self._journal()
        self.assertEqual(1, len(journal))
        self.assertEqual("inside", journal[0].before_content)


class UndoSnapshotSemanticsTest(_FsToolCase):
    """`kernel_read_undo_snapshot` 的语义与旧前置读逐字对齐（除有意差异）。"""

    def _snapshot(self, relative: str):
        binding = self.resolver.require_binding(self.conversation.id)
        resolved = resolve_workspace_path(binding.root, relative)
        return kernel_read_undo_snapshot(binding, resolved)

    def test_snapshot_reads_normally_before_any_swap(self) -> None:
        """正向对照：没有换链时路径本来是通的。"""
        self._write("sub/data.txt", "inside")

        self.assertEqual(("inside", b"inside"), self._snapshot("sub/data.txt"))

    def test_snapshot_rejects_leaf_symlink_swap(self) -> None:
        target = self._write("sub/data.txt", "inside")
        secret = self._outside_secret()
        binding = self.resolver.require_binding(self.conversation.id)
        resolved = resolve_workspace_path(binding.root, "sub/data.txt")
        target.unlink()
        target.symlink_to(secret)

        with self.assertRaises(ToolError):
            kernel_read_undo_snapshot(binding, resolved)

    def test_snapshot_rejects_intermediate_symlink_swap(self) -> None:
        self._write("sub/data.txt", "inside")
        secret = self._outside_secret()
        binding = self.resolver.require_binding(self.conversation.id)
        resolved = resolve_workspace_path(binding.root, "sub/data.txt")
        real = self.root / "sub_real"
        os.rename(self.root / "sub", real)
        os.symlink(str(secret.parent), str(self.root / "sub"))

        with self.assertRaises(ToolError):
            kernel_read_undo_snapshot(binding, resolved)

    def test_missing_file_yields_empty_before(self) -> None:
        """新建文件没有 before —— 与旧前置读的 None 语义一致。"""
        self.assertEqual((None, None), self._snapshot("new.txt"))

    def test_binary_keeps_raw_but_no_text(self) -> None:
        path = self.root / "b.bin"
        path.write_bytes(b"\xff\xfe\x00")

        self.assertEqual((None, b"\xff\xfe\x00"), self._snapshot("b.bin"))

    def test_oversized_keeps_raw_but_no_text(self) -> None:
        path = self.root / "big.txt"
        raw = b"x" * 520_000
        path.write_bytes(raw)

        self.assertEqual((None, raw), self._snapshot("big.txt"))

    def test_crlf_is_normalized_like_read_text(self) -> None:
        """换行口径与 `Path.read_text` 的 universal newlines 逐字一致。"""
        raw = b"a\r\nb\rc"
        (self.root / "crlf.txt").write_bytes(raw)

        self.assertEqual(("a\nb\nc", raw), self._snapshot("crlf.txt"))


class NoPathPreReadScanTest(unittest.TestCase):
    """静态守卫：undo/diff 前置读不许退回按路径（覆盖行为锚点够不到的调用点）。

    扫描面 = `fs/common.py` + 四个前置读工具。read_workspace_file / read_skill_file
    **不在扫描面**：它们的注释里刻意引用旧模式作历史说明（C3/C4 已闭）。
    """

    NEEDLES = (
        "_read_text_or_none",
        "canonical.read_bytes()",
        "canonical.read_text(",
        "canonical.open(",
    )
    SCAN_DIR = Path(__file__).resolve().parent.parent / "src" / "endless_task" / "workspace_runtime" / "fs"
    SCANNED = (
        "common.py",
        "write_workspace_file.py",
        "delete_workspace_file.py",
        "manage_workspace_paths.py",
        "edit_workspace_file.py",
    )

    def _violations(self, text: str) -> list:
        return [needle for needle in self.NEEDLES if needle in text]

    def test_fs_tools_have_no_path_based_preread(self) -> None:
        self.assertGreaterEqual(len(self.SCANNED), 5)
        for name in self.SCANNED:
            path = self.SCAN_DIR / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 200, f"{name} 读到空/过短，扫描在空转")
            self.assertEqual([], self._violations(text), f"{name} 仍按路径做前置读")

    def test_scanner_catches_planted_needles(self) -> None:
        """用例内正向对照：判据被弄坏时，这个用例自己红。"""
        self.assertEqual(
            ["_read_text_or_none"], self._violations("x = _read_text_or_none(p)")
        )
        self.assertEqual(
            ["canonical.read_bytes()"],
            self._violations("raw = resolved.canonical.read_bytes()"),
        )
        self.assertEqual([], self._violations("raw = kernel_read(binding, r, n)"))


if __name__ == "__main__":
    unittest.main()
