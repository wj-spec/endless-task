"""S7 类型化文件动词：edit_workspace_file / manage_workspace_paths。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteUndoJournalRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import (
    EditWorkspaceFileTool,
    ManageWorkspacePathsTool,
)
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.undo_service import WorkspaceUndoService


def _call(conversation_id: str, **arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="tool",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-09T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


class _FsToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "edit.db")
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
        self.edit = EditWorkspaceFileTool(
            self.resolver, self.effect_log, undo_service=self.undo
        )
        self.paths = ManageWorkspacePathsTool(
            self.resolver, self.effect_log, undo_service=self.undo
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _journal(self):
        return self.undo_repository.list_for_conversation(self.conversation.id)


class EditWorkspaceFileTest(_FsToolCase):
    def test_unique_match_replaces_one_occurrence_and_returns_diff(self) -> None:
        target = self._write("notes.md", "标题\n旧内容\n结尾\n")

        result = asyncio.run(
            self.edit.execute(
                _call(
                    self.conversation.id,
                    path="notes.md",
                    old_string="旧内容",
                    new_string="新内容",
                ),
                _token(),
            )
        )

        self.assertEqual("标题\n新内容\n结尾\n", target.read_text(encoding="utf-8"))
        self.assertEqual(1, result.structured_content["replaced"])
        self.assertIn("- 旧内容", result.content)
        self.assertIn("+ 新内容", result.content)
        self.assertEqual("file_write", result.structured_content["effect"]["kind"])

    def test_no_match_is_rejected(self) -> None:
        self._write("notes.md", "内容\n")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="notes.md",
                        old_string="不存在",
                        new_string="x",
                    ),
                    _token(),
                )
            )

        self.assertEqual("edit_no_match", ctx.exception.code)

    def test_multiple_matches_require_replace_all_and_report_lines(self) -> None:
        self._write("notes.md", "a\nb\na\n")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="notes.md",
                        old_string="a",
                        new_string="z",
                    ),
                    _token(),
                )
            )

        self.assertEqual("edit_not_unique", ctx.exception.code)
        self.assertIn("L1", str(ctx.exception))
        self.assertIn("L3", str(ctx.exception))

        result = asyncio.run(
            self.edit.execute(
                _call(
                    self.conversation.id,
                    path="notes.md",
                    old_string="a",
                    new_string="z",
                    replace_all=True,
                ),
                _token(),
            )
        )
        self.assertEqual(2, result.structured_content["replaced"])
        self.assertEqual("z\nb\nz\n", (self.root / "notes.md").read_text(encoding="utf-8"))

    def test_no_change_is_rejected(self) -> None:
        self._write("notes.md", "内容\n")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="notes.md",
                        old_string="内容",
                        new_string="内容",
                    ),
                    _token(),
                )
            )

        self.assertEqual("edit_no_change", ctx.exception.code)

    def test_path_guards(self) -> None:
        self._write("notes.md", "内容\n")
        (self.root / "sub").mkdir()

        with self.assertRaises(ToolError) as escape:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="../x.md",
                        old_string="a",
                        new_string="b",
                    ),
                    _token(),
                )
            )
        self.assertEqual("path_escape", escape.exception.code)

        with self.assertRaises(ToolError) as missing:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="nope.md",
                        old_string="a",
                        new_string="b",
                    ),
                    _token(),
                )
            )
        self.assertEqual("path_not_found", missing.exception.code)

        with self.assertRaises(ToolError) as directory:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="sub",
                        old_string="a",
                        new_string="b",
                    ),
                    _token(),
                )
            )
        self.assertEqual("path_is_directory", directory.exception.code)

    def test_binary_file_is_rejected(self) -> None:
        (self.root / "blob.bin").write_bytes(b"\xff\xfe\x00")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.edit.execute(
                    _call(
                        self.conversation.id,
                        path="blob.bin",
                        old_string="a",
                        new_string="b",
                    ),
                    _token(),
                )
            )

        self.assertEqual("binary_content", ctx.exception.code)

    def test_records_undo_and_effect_log(self) -> None:
        self._write("notes.md", "旧内容\n")

        asyncio.run(
            self.edit.execute(
                _call(
                    self.conversation.id,
                    path="notes.md",
                    old_string="旧",
                    new_string="新",
                ),
                _token(),
            )
        )

        entries = self._journal()
        self.assertEqual(1, len(entries))
        self.assertEqual("file_write", entries[0].kind)
        self.assertEqual("notes.md", entries[0].target)
        self.assertEqual("旧内容\n", entries[0].before_content)

        entry, performed = self.undo.undo(entries[0].id)
        self.assertTrue(performed)
        self.assertEqual("旧内容\n", (self.root / "notes.md").read_text(encoding="utf-8"))

    def test_does_not_require_confirmation(self) -> None:
        self.assertFalse(
            self.edit.requires_explicit_confirmation(
                _call(self.conversation.id, path="notes.md")
            )
        )


class ManageWorkspacePathsTest(_FsToolCase):
    def test_mkdir_creates_nested_directory_and_is_idempotent(self) -> None:
        result = asyncio.run(
            self.paths.execute(
                _call(self.conversation.id, operation="mkdir", path="docs/notes"),
                _token(),
            )
        )
        self.assertTrue((self.root / "docs" / "notes").is_dir())
        self.assertTrue(result.structured_content["created"])

        again = asyncio.run(
            self.paths.execute(
                _call(self.conversation.id, operation="mkdir", path="docs/notes"),
                _token(),
            )
        )
        self.assertFalse(again.structured_content["created"])

    def test_mkdir_rejects_existing_file(self) -> None:
        self._write("docs", "not a dir")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.paths.execute(
                    _call(self.conversation.id, operation="mkdir", path="docs"),
                    _token(),
                )
            )
        self.assertEqual("path_is_file", ctx.exception.code)

    def test_copy_creates_new_file_with_undo(self) -> None:
        self._write("src/a.md", "内容\n")

        result = asyncio.run(
            self.paths.execute(
                _call(
                    self.conversation.id,
                    operation="copy",
                    path="src/a.md",
                    to_path="dst/a.md",
                ),
                _token(),
            )
        )

        self.assertEqual("内容\n", (self.root / "dst" / "a.md").read_text(encoding="utf-8"))
        self.assertEqual("copy", result.structured_content["operation"])
        entries = self._journal()
        self.assertEqual(1, len(entries))
        self.assertFalse(entries[0].before_exists)
        self.undo.undo(entries[0].id)
        self.assertFalse((self.root / "dst" / "a.md").exists())

    def test_copy_refuses_existing_target_without_overwrite(self) -> None:
        self._write("a.md", "新\n")
        self._write("b.md", "旧\n")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.paths.execute(
                    _call(
                        self.conversation.id,
                        operation="copy",
                        path="a.md",
                        to_path="b.md",
                    ),
                    _token(),
                )
            )
        self.assertEqual("target_exists", ctx.exception.code)
        self.assertEqual("旧\n", (self.root / "b.md").read_text(encoding="utf-8"))

    def test_copy_overwrite_requires_confirmation_and_is_undoable(self) -> None:
        self._write("a.md", "新\n")
        self._write("b.md", "旧\n")

        call = _call(
            self.conversation.id,
            operation="copy",
            path="a.md",
            to_path="b.md",
            overwrite=True,
        )
        self.assertTrue(self.paths.requires_explicit_confirmation(call))

        asyncio.run(self.paths.execute(call, _token()))

        self.assertEqual("新\n", (self.root / "b.md").read_text(encoding="utf-8"))
        entry = self._journal()[0]
        self.assertTrue(entry.before_exists)
        self.undo.undo(entry.id)
        self.assertEqual("旧\n", (self.root / "b.md").read_text(encoding="utf-8"))

    def test_move_relocates_file_with_two_step_undo(self) -> None:
        self._write("old/note.md", "内容\n")

        result = asyncio.run(
            self.paths.execute(
                _call(
                    self.conversation.id,
                    operation="move",
                    path="old/note.md",
                    to_path="new/note.md",
                ),
                _token(),
            )
        )

        self.assertFalse((self.root / "old" / "note.md").exists())
        self.assertEqual("内容\n", (self.root / "new" / "note.md").read_text(encoding="utf-8"))
        self.assertEqual("move", result.structured_content["operation"])
        entries = self._journal()
        self.assertEqual({"file_write", "file_delete"}, {item.kind for item in entries})

        for item in entries:
            self.undo.undo(item.id)
        self.assertEqual("内容\n", (self.root / "old" / "note.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "new" / "note.md").exists())

    def test_move_rejects_directory_and_escape(self) -> None:
        (self.root / "sub").mkdir()
        self._write("a.md", "内容\n")

        with self.assertRaises(ToolError) as directory:
            asyncio.run(
                self.paths.execute(
                    _call(
                        self.conversation.id,
                        operation="move",
                        path="sub",
                        to_path="sub2",
                    ),
                    _token(),
                )
            )
        self.assertEqual("path_is_directory", directory.exception.code)

        with self.assertRaises(ToolError) as escape:
            asyncio.run(
                self.paths.execute(
                    _call(
                        self.conversation.id,
                        operation="move",
                        path="a.md",
                        to_path="../evil.md",
                    ),
                    _token(),
                )
            )
        self.assertEqual("path_escape", escape.exception.code)

    def test_copy_rejects_binary_overwrite(self) -> None:
        self._write("a.md", "新\n")
        (self.root / "b.bin").write_bytes(b"\xff\xfe\x00")

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.paths.execute(
                    _call(
                        self.conversation.id,
                        operation="copy",
                        path="a.md",
                        to_path="b.bin",
                        overwrite=True,
                    ),
                    _token(),
                )
            )
        self.assertEqual("binary_overwrite_unsupported", ctx.exception.code)
        self.assertEqual(b"\xff\xfe\x00", (self.root / "b.bin").read_bytes())

    def test_unknown_operation_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self.paths.execute(
                    _call(self.conversation.id, operation="rename", path="a.md"),
                    _token(),
                )
            )
        self.assertEqual("invalid_operation", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
