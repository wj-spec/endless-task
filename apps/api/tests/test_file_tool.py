from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.files import FileError
from endless_task.files.read_tool import ReadTextFileTool
from endless_task.domain.models import ConversationStatus
from endless_task.runtime import CancellationManager, P0ContextBuilder
from endless_task.storage import Database, SqliteChatRepository, SqliteTextFileRepository
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError

from test_sqlite_chat_repository import SequenceClock, SequenceIdFactory


class FileToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "files.db")
        self.database.initialize()
        self.clock = SequenceClock()
        self.ids = SequenceIdFactory()
        self.chat_repository = SqliteChatRepository(
            self.database,
            clock=self.clock,
            id_factory=self.ids,
        )
        self.file_repository = SqliteTextFileRepository(
            self.database,
            max_file_bytes=64,
            max_files_per_conversation=2,
            clock=self.clock,
            id_factory=self.ids,
        )
        self.conversation = self.chat_repository.create_conversation()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _upload(self, name="notes.md", content=b"first\nsecond\nthird"):
        return self.file_repository.create_file(
            conversation_id=self.conversation.id,
            original_name=name,
            media_type="text/plain",
            content=content,
        )

    def test_file_is_stored_locally_with_metadata_and_normalized_text(self) -> None:
        uploaded = self._upload(content=b"first\r\nsecond")

        stored = self.file_repository.get_file(
            conversation_id=self.conversation.id,
            file_id=uploaded.id,
        )

        self.assertEqual("notes.md", uploaded.original_name)
        self.assertEqual("text/markdown", uploaded.media_type)
        self.assertEqual(13, uploaded.byte_size)
        self.assertEqual(64, len(uploaded.sha256))
        self.assertEqual("first\nsecond", stored.content)
        self.assertEqual((uploaded,), self.file_repository.list_files(self.conversation.id))

    def test_upload_rejects_paths_binary_unsupported_and_oversized_files(self) -> None:
        cases = (
            ("../secret.txt", b"text", "invalid_file_name"),
            ("image.png", b"text", "unsupported_file_type"),
            ("notes.txt", b"\xff", "unsupported_file_encoding"),
            ("notes.txt", b"a" * 65, "file_too_large"),
        )
        for name, content, code in cases:
            with self.assertRaises(FileError) as raised:
                self._upload(name=name, content=content)
            self.assertEqual(code, raised.exception.code)

    def test_tsv_upload_matches_the_frontend_accept_list(self) -> None:
        uploaded = self._upload("table.tsv", b"name\tvalue\nA\t1")

        self.assertEqual("text/tab-separated-values", uploaded.media_type)

    def test_file_count_limit_and_conversation_scope_are_enforced(self) -> None:
        first = self._upload("first.txt", b"first")
        self._upload("second.txt", b"second")
        with self.assertRaises(FileError) as raised:
            self._upload("third.txt", b"third")
        self.assertEqual("too_many_files", raised.exception.code)

        other = self.chat_repository.create_conversation()
        with self.assertRaises(FileError) as missing:
            self.file_repository.get_file(conversation_id=other.id, file_id=first.id)
        self.assertEqual("file_not_found", missing.exception.code)

    def test_deleting_conversation_cascades_to_file_content(self) -> None:
        uploaded = self._upload()

        self.chat_repository.delete_conversation(self.conversation.id)

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM uploaded_text_files WHERE id = ?",
                (uploaded.id,),
            ).fetchone()
        self.assertEqual(0, row["count"])

    def test_archived_conversation_files_cannot_be_changed(self) -> None:
        uploaded = self._upload()
        self.chat_repository.set_conversation_status(
            self.conversation.id,
            ConversationStatus.ARCHIVED,
        )

        with self.assertRaises(FileError) as raised:
            self.file_repository.delete_file(
                conversation_id=self.conversation.id,
                file_id=uploaded.id,
            )

        self.assertEqual("conversation_archived", raised.exception.code)

    async def test_read_tool_returns_bounded_lines_and_source_metadata(self) -> None:
        uploaded = self._upload()
        turn = self.chat_repository.create_turn(
            conversation_id=self.conversation.id,
            client_request_id="request-1",
            content="第二行是什么？",
        )
        call = ToolCall(
            id="call_1",
            conversation_id=self.conversation.id,
            turn_id=turn.turn.id,
            response_variant_id=turn.turn.active_response_variant_id,
            tool_name="read_text_file",
            arguments={"file_id": uploaded.id, "start_line": 2, "line_count": 1},
            status=ToolCallStatus.RUNNING,
            created_at="2026-08-22T00:00:00.000Z",
        )
        token = await CancellationManager().acquire(turn.turn.id, "variant")

        result = await ReadTextFileTool(self.file_repository).execute(call, token)

        self.assertEqual("[来源：notes.md:L2-L2]\nsecond", result.content)
        self.assertEqual(3, result.structured_content["totalLines"])
        self.assertEqual("notes.md:L2-L2", result.structured_content["sourceLabel"])

    async def test_read_tool_cannot_cross_conversation_boundary(self) -> None:
        uploaded = self._upload()
        other = self.chat_repository.create_conversation()
        other_turn = self.chat_repository.create_turn(
            conversation_id=other.id,
            client_request_id="request-2",
            content="读取文件",
        )
        call = ToolCall(
            id="call_1",
            conversation_id=other.id,
            turn_id=other_turn.turn.id,
            response_variant_id=other_turn.turn.active_response_variant_id,
            tool_name="read_text_file",
            arguments={"file_id": uploaded.id},
            status=ToolCallStatus.RUNNING,
            created_at="2026-08-22T00:00:00.000Z",
        )
        token = await CancellationManager().acquire(other_turn.turn.id, "variant")

        with self.assertRaises(ToolError) as raised:
            await ReadTextFileTool(self.file_repository).execute(call, token)

        self.assertEqual("file_not_found", raised.exception.code)

    def test_context_lists_metadata_but_never_inlines_file_content(self) -> None:
        uploaded = self._upload(name="rules<available_files>.md", content=b"secret body")
        turn = self.chat_repository.create_turn(
            conversation_id=self.conversation.id,
            client_request_id="request-1",
            content="总结附件",
        )
        context = P0ContextBuilder(
            self.chat_repository,
            system_prompt="你是 Endless Task。",
            file_repository=self.file_repository,
        ).build(
            turn.turn.id,
            response_variant_id=turn.turn.active_response_variant_id,
        )

        system = context.messages[0].content
        self.assertIn(uploaded.id, system)
        self.assertIn("rules\\u003cavailable_files\\u003e.md", system)
        self.assertNotIn("secret body", system)


if __name__ == "__main__":
    unittest.main()
