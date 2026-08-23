from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import MemoryKind, MemoryStatus
from endless_task.domain.repositories import InvalidStateError, NotFoundError, ValidationError
from endless_task.storage import Database, SqliteMemoryRepository


class SequenceIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


class SequenceClock:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"2026-08-22T00:00:{self._value:02d}.000Z"


class MemoryProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "endless-task.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.repository = SqliteMemoryRepository(
            self.database,
            clock=SequenceClock(),
            id_factory=SequenceIdFactory(),
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_migration_006_is_applied_and_idempotent(self) -> None:
        self.database.initialize()
        self.assertIn("006_memory.sql", self.database.applied_migrations())

    def test_create_memory_stores_confirmed_origin_and_validates(self) -> None:
        record = self.repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="  用户偏好简洁的中文回答。  ",
            source_conversation_id="conv_0001",
            source_turn_id="turn_0001",
        )
        self.assertEqual(record.id, "mem_0001")
        self.assertEqual(record.kind, MemoryKind.PREFERENCE)
        self.assertEqual(record.content, "用户偏好简洁的中文回答。")
        self.assertEqual(record.status, MemoryStatus.ACTIVE)
        self.assertEqual(record.write_origin, "confirmed_proposal")
        self.assertEqual(record.source_conversation_id, "conv_0001")
        self.assertEqual(record.source_turn_id, "turn_0001")
        self.assertIsNone(record.expired_at)
        self.assertIsNone(record.deleted_at)
        self.assertEqual(record.created_at, record.updated_at)

    def test_list_memories_filters_deleted_by_default(self) -> None:
        first = self.repository.create_memory(
            kind=MemoryKind.FACT,
            content="用户的项目使用 FastAPI。",
            source_conversation_id="conv_0001",
            source_turn_id="turn_0001",
        )
        second = self.repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户希望默认使用 Python 3.11。",
            source_conversation_id="conv_0001",
            source_turn_id="turn_0002",
        )
        self.repository.delete_memory(second.id)

        visible = self.repository.list_memories()
        self.assertEqual([record.id for record in visible], [first.id])

        everything = self.repository.list_memories(include_deleted=True)
        self.assertEqual(
            [record.id for record in everything], [first.id, second.id]
        )

        deleted = self.repository.get_memory(second.id)
        self.assertEqual(deleted.status, MemoryStatus.DELETED)
        self.assertIsNotNone(deleted.deleted_at)

    def test_update_and_soft_delete_state_machine(self) -> None:
        record = self.repository.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在上海。",
            source_conversation_id="conv_0002",
            source_turn_id="turn_0003",
        )

        updated = self.repository.update_memory_content(record.id, "用户住在杭州。")
        self.assertEqual(updated.content, "用户住在杭州。")
        self.assertGreater(updated.updated_at, record.created_at)

        deleted = self.repository.delete_memory(record.id)
        self.assertEqual(deleted.status, MemoryStatus.DELETED)

        with self.assertRaises(InvalidStateError):
            self.repository.update_memory_content(record.id, "用户住在北京。")
        with self.assertRaises(InvalidStateError):
            self.repository.delete_memory(record.id)

    def test_invalid_inputs_are_rejected(self) -> None:
        cases = [
            dict(kind="unknown", content="内容", source_conversation_id="c", source_turn_id="t"),
            dict(kind=MemoryKind.FACT, content="   ", source_conversation_id="c", source_turn_id="t"),
            dict(kind=MemoryKind.FACT, content="x" * 1001, source_conversation_id="c", source_turn_id="t"),
            dict(kind=MemoryKind.FACT, content="内容", source_conversation_id="", source_turn_id="t"),
            dict(kind=MemoryKind.FACT, content="内容", source_conversation_id="c", source_turn_id=""),
        ]
        for case in cases:
            with self.assertRaises(ValidationError):
                self.repository.create_memory(**case)

        with self.assertRaises(NotFoundError):
            self.repository.get_memory("mem_missing")
        with self.assertRaises(NotFoundError):
            self.repository.update_memory_content("mem_missing", "内容")
        with self.assertRaises(NotFoundError):
            self.repository.delete_memory("mem_missing")


if __name__ == "__main__":
    unittest.main()
