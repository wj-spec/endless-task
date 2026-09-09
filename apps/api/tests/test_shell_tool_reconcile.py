"""S8 shell 变更对账：run_shell 造成的文件改动进撤销日志与审计日志。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteUndoJournalRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.shell_tool import RunShellTool
from endless_task.workspace_runtime.undo_service import WorkspaceUndoService


def _call(conversation_id: str, command: str) -> ToolCall:
    return ToolCall(
        id="call_shell",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="run_shell",
        arguments={"command": command},
        status=ToolCallStatus.CREATED,
        created_at="2026-09-09T00:00:00.000Z",
    )


class ShellReconcileToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "shell.db")
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
        self.undo = WorkspaceUndoService(
            repository=SqliteUndoJournalRepository(self.database)
        )
        self.coordinator = RunCheckpointCoordinator(
            store_root=Path(self._temp.name) / "checkpoints"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _tool(self, **kwargs) -> RunShellTool:
        return RunShellTool(
            self.resolver,
            self.effect_log,
            checkpoint_coordinator=self.coordinator,
            undo_service=self.undo,
            **kwargs,
        )

    def _run(self, tool: RunShellTool, command: str):
        return asyncio.run(
            tool.execute(_call(self.conversation.id, command), CancellationToken())
        )

    def _journal(self):
        return list(self.undo._repository.list_for_conversation(self.conversation.id))

    def _effects(self):
        return self.effect_log.list_for_workspace(self.workspace.id, limit=20)

    def test_created_file_is_recorded_and_undoable(self) -> None:
        result = self._run(self._tool(), "printf 'hello' > out.txt")

        self.assertEqual("hello", (self.root / "out.txt").read_text(encoding="utf-8"))
        self.assertIn("out.txt", result.structured_content["changedPaths"])
        self.assertIn("改动了工作区文件", result.content)

        entries = self._journal()
        self.assertEqual(1, len(entries))
        self.assertFalse(entries[0].before_exists)
        self.assertEqual("out.txt", entries[0].target)

        self.undo.undo(entries[0].id)
        self.assertFalse((self.root / "out.txt").exists())

        operations = [entry["operation"] for entry in self._effects()]
        self.assertIn("shell_file_write", operations)

    def test_modified_file_undo_restores_previous_content(self) -> None:
        (self.root / "note.md").write_text("旧内容\n", encoding="utf-8")

        result = self._run(self._tool(), "printf '新内容\\n' > note.md")

        self.assertEqual("新内容\n", (self.root / "note.md").read_text(encoding="utf-8"))
        self.assertIn("note.md", result.structured_content["changedPaths"])

        entry = self._journal()[0]
        self.assertTrue(entry.before_exists)
        self.assertEqual("旧内容\n", entry.before_content)

        self.undo.undo(entry.id)
        self.assertEqual("旧内容\n", (self.root / "note.md").read_text(encoding="utf-8"))

    def test_deleted_file_undo_restores_from_checkpoint(self) -> None:
        (self.root / "gone.md").write_text("要恢复的内容\n", encoding="utf-8")

        result = self._run(self._tool(), "rm gone.md")

        self.assertFalse((self.root / "gone.md").exists())
        self.assertIn("gone.md", result.structured_content["changedPaths"])

        entries = self._journal()
        self.assertEqual("file_delete", entries[0].kind)
        self.assertEqual("要恢复的内容\n", entries[0].before_content)

        self.undo.undo(entries[0].id)
        self.assertEqual(
            "要恢复的内容\n", (self.root / "gone.md").read_text(encoding="utf-8")
        )
        operations = [entry["operation"] for entry in self._effects()]
        self.assertIn("shell_file_delete", operations)

    def test_read_only_command_reports_no_changes(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")

        result = self._run(self._tool(), "ls")

        self.assertEqual([], result.structured_content["changedPaths"])
        self.assertEqual([], self._journal())
        self.assertNotIn("改动了工作区文件", result.content)

    def test_over_budget_skips_reconciliation_explicitly(self) -> None:
        for index in range(3):
            (self.root / f"f{index}.txt").write_text("x", encoding="utf-8")

        result = self._run(self._tool(reconcile_max_files=1), "printf 'y' > new.txt")

        self.assertEqual("workspace_too_large", result.structured_content["reconcileSkipped"])
        self.assertIn("未做文件变更对账", result.content)
        self.assertEqual([], self._journal())


if __name__ == "__main__":
    unittest.main()
