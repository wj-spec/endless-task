"""A5 撤销/回滚：文件写/删的撤销语义与幂等。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.storage import (
    Database,
    SqliteUndoJournalRepository,
)
from endless_task.workspace_runtime.undo_service import (
    UndoUnavailableError,
    WorkspaceUndoService,
)


class _RecordingEffectLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, **kwargs) -> None:
        self.entries.append(kwargs)


class _UndoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.database = Database(Path(self._tmp.name) / "undo.db")
        self.database.initialize()
        self.repository = SqliteUndoJournalRepository(self.database)
        self.effect_log = _RecordingEffectLog()
        self.service = WorkspaceUndoService(
            repository=self.repository,
            effect_log=self.effect_log,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class RecordAndUndoTest(_UndoTestCase):
    def test_undo_restores_overwritten_content(self) -> None:
        path = self._write("notes/plan.md", "旧内容")
        entry = self.service.record_file_write(
            conversation_id="conv_1",
            target="notes/plan.md",
            workspace_root=str(self.root),
            before_content="旧内容",
            before_exists=True,
        )
        assert entry is not None
        path.write_text("新内容", encoding="utf-8")

        undone, performed = self.service.undo(entry.id)
        self.assertTrue(performed)
        self.assertFalse(undone.undoable)
        self.assertEqual("旧内容", path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.effect_log.entries))
        self.assertEqual("undo", self.effect_log.entries[0]["operation"])

    def test_undo_deletes_newly_created_file(self) -> None:
        entry = self.service.record_file_write(
            conversation_id="conv_1",
            target="notes/new.md",
            workspace_root=str(self.root),
            before_content=None,
            before_exists=False,
        )
        assert entry is not None
        path = self._write("notes/new.md", "agent 写入")

        _, performed = self.service.undo(entry.id)
        self.assertTrue(performed)
        self.assertFalse(path.exists())

    def test_undo_restores_deleted_file(self) -> None:
        entry = self.service.record_file_delete(
            conversation_id="conv_1",
            target="notes/removed.md",
            workspace_root=str(self.root),
            before_content="被删掉的内容",
        )
        assert entry is not None

        _, performed = self.service.undo(entry.id)
        self.assertTrue(performed)
        restored = self.root / "notes" / "removed.md"
        self.assertEqual("被删掉的内容", restored.read_text(encoding="utf-8"))

    def test_undo_is_idempotent(self) -> None:
        path = self._write("a.txt", "旧")
        entry = self.service.record_file_write(
            conversation_id="conv_1",
            target="a.txt",
            workspace_root=str(self.root),
            before_content="旧",
            before_exists=True,
        )
        assert entry is not None
        path.write_text("新", encoding="utf-8")
        self.service.undo(entry.id)
        # 第二次撤销不再动文件（哪怕用户之后又改了它）。
        path.write_text("用户后来改的", encoding="utf-8")
        undone, performed = self.service.undo(entry.id)
        self.assertFalse(performed)
        self.assertFalse(undone.undoable)
        self.assertEqual("用户后来改的", path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.effect_log.entries))

    def test_journal_failure_does_not_raise(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("db down")

        self.repository.append = boom  # type: ignore[assignment]
        self.assertIsNone(
            self.service.record_file_write(
                conversation_id="conv_1",
                target="a.txt",
                workspace_root=str(self.root),
                before_content=None,
                before_exists=False,
            )
        )

    def test_unknown_entry_raises_not_found(self) -> None:
        with self.assertRaises(Exception):
            self.service.undo("undo_missing")

    def test_path_escape_is_rejected(self) -> None:
        entry = self.repository.append(
            conversation_id="conv_1",
            kind="file_write",
            target="../outside.txt",
            workspace_root=str(self.root),
            before_exists=True,
            before_content="x",
            description="越界写入",
        )
        with self.assertRaises(Exception):
            self.service.undo(entry.id)

    def test_unsupported_kind_is_refused(self) -> None:
        entry = self.repository.append(
            conversation_id="conv_1",
            kind="file_write",
            target="a.txt",
            workspace_root=str(self.root),
            before_exists=True,
            before_content="x",
            description="x",
        )
        object.__setattr__(entry, "kind", "shell")
        self.repository.get = lambda entry_id: entry  # type: ignore[assignment]
        with self.assertRaises(UndoUnavailableError):
            self.service.undo(entry.id)


class JournalQueryTest(_UndoTestCase):
    def test_latest_available_skips_undone(self) -> None:
        first = self.service.record_file_write(
            conversation_id="conv_1",
            target="a.txt",
            workspace_root=str(self.root),
            before_content=None,
            before_exists=False,
        )
        assert first is not None
        self.assertEqual(first.id, self.repository.latest_available("conv_1").id)
        self.repository.mark_undone(first.id)
        self.assertIsNone(self.repository.latest_available("conv_1"))

    def test_list_is_scoped_to_conversation_and_desc(self) -> None:
        for index in range(3):
            self.service.record_file_write(
                conversation_id="conv_1",
                target=f"f{index}.txt",
                workspace_root=str(self.root),
                before_content=None,
                before_exists=False,
            )
        self.service.record_file_write(
            conversation_id="conv_2",
            target="other.txt",
            workspace_root=str(self.root),
            before_content=None,
            before_exists=False,
        )
        entries = self.repository.list_for_conversation("conv_1")
        self.assertEqual(3, len(entries))
        self.assertEqual("f2.txt", entries[0].target)


if __name__ == "__main__":
    unittest.main()
