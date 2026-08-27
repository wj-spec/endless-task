"""R5.9 长文分块：file 源按段落/小节切分为检索单元。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from endless_task.domain.models import (
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.storage import Database, SqliteKnowledgeRepository
from endless_task.storage.knowledge_chunking import chunk_text


class ChunkTextTest(unittest.TestCase):
    def test_merges_short_paragraphs(self) -> None:
        chunks = chunk_text("甲段。\n\n乙段。", max_chars=800)
        self.assertEqual(chunks, ["甲段。\n\n乙段。"])

    def test_heading_starts_new_chunk(self) -> None:
        content = "引言。\n\n# 第一节\n第一节内容。\n\n# 第二节\n第二节内容。"
        chunks = chunk_text(content, max_chars=800)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[1].startswith("# 第一节"))
        self.assertTrue(chunks[2].startswith("# 第二节"))

    def test_long_paragraph_is_hard_split(self) -> None:
        chunks = chunk_text("字" * 2500, max_chars=800)
        self.assertEqual(sum(len(chunk) for chunk in chunks), 2500)
        self.assertTrue(all(len(chunk) <= 800 for chunk in chunks))

    def test_empty_content_returns_no_chunks(self) -> None:
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   \n\n  "), [])


class RecordingHook:
    def __init__(self) -> None:
        self.submitted: List[str] = []
        self.removed: List[str] = []

    def submit(self, scope: str, ref_id: str) -> None:
        self.submitted.append(ref_id)

    def remove(self, scope: str, ref_id: str) -> None:
        self.removed.append(ref_id)


class KnowledgeChunksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "chunks.db")
        self.database.initialize()
        self.repository = SqliteKnowledgeRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _make_file_source(self, content: str, title: str = "长文件"):
        return self.repository.create_source(
            kind=KnowledgeSourceKind.FILE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
            file_name="doc.md",
        )

    def test_file_source_is_chunked_note_is_not(self) -> None:
        file_source = self._make_file_source(
            "第一段落。\n\n# 小节一\n小节一内容。\n\n# 小节二\n小节二内容。"
        )
        self.assertEqual(len(self.repository.list_chunk_ids(file_source.id)), 3)
        note = self.repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="笔记",
            content="一段笔记。",
        )
        self.assertEqual(self.repository.list_chunk_ids(note.id), [])

    def test_search_hits_carry_chunk_provenance(self) -> None:
        source = self._make_file_source(
            "咖啡机简介。\n\n# 奶管清洗\n使用后必须清洗奶管。\n\n# 其他保养\n定期除垢。"
        )
        hits = self.repository.search("奶管清洗", [KnowledgeScope.SOURCE])
        hit_list = hits[KnowledgeScope.SOURCE]
        self.assertEqual(len(hit_list), 1)
        hit = hit_list[0]
        self.assertEqual(hit.source_id, source.id)
        self.assertEqual(hit.chunk_seq, 1)
        self.assertEqual(hit.title, "长文件")
        self.assertIn("奶管", hit.snippet)
        self.assertNotIn("除垢", hit.snippet)

    def test_update_replaces_chunks(self) -> None:
        source = self._make_file_source("旧内容。\n\n# 旧小节\n旧小节内容。")
        old_ids = self.repository.list_chunk_ids(source.id)
        self.repository.update_source(source.id, content="全新的一段内容。")
        new_ids = self.repository.list_chunk_ids(source.id)
        self.assertEqual(len(new_ids), 1)
        self.assertNotEqual(old_ids, new_ids)
        self.assertEqual(
            self.repository.search("旧小节", [KnowledgeScope.SOURCE]), {}
        )

    def test_expired_file_source_chunks_are_invisible(self) -> None:
        source = self._make_file_source("# 小节\n小节内容在这里。")
        self.assertTrue(self.repository.search("小节", [KnowledgeScope.SOURCE]))
        self.repository.expire_source(source.id)
        self.assertEqual(
            self.repository.search("小节", [KnowledgeScope.SOURCE]), {}
        )
        self.repository.restore_source(source.id)
        self.assertTrue(self.repository.search("小节", [KnowledgeScope.SOURCE]))

    def test_legacy_file_source_without_chunks_is_searchable(self) -> None:
        source = self._make_file_source("遗留文件的完整内容。")
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM knowledge_chunks WHERE source_id = ?", (source.id,)
            )
        hits = self.repository.search("遗留文件", [KnowledgeScope.SOURCE])
        hit_list = hits[KnowledgeScope.SOURCE]
        self.assertEqual(hit_list[0].ref_id, source.id)
        self.assertIsNone(hit_list[0].chunk_seq)

    def test_hooks_submit_chunk_ids_and_clean_stale(self) -> None:
        hook = RecordingHook()
        self.repository.set_embedding_hook(hook)
        source = self._make_file_source("甲段。\n\n# 小节一\n小节一内容。")
        chunk_ids = self.repository.list_chunk_ids(source.id)
        self.assertEqual(sorted(hook.submitted), sorted(chunk_ids))

        hook.submitted.clear()
        hook.removed.clear()
        self.repository.update_source(source.id, content="全新内容。")
        new_ids = self.repository.list_chunk_ids(source.id)
        self.assertEqual(sorted(hook.submitted), sorted(new_ids))
        self.assertEqual(sorted(hook.removed), sorted(chunk_ids))

        hook.submitted.clear()
        hook.removed.clear()
        self.repository.delete_source(source.id)
        self.assertEqual(hook.submitted, [])
        self.assertEqual(
            sorted(hook.removed), sorted([source.id] + new_ids)
        )

    def test_note_hook_still_submits_source_id(self) -> None:
        hook = RecordingHook()
        self.repository.set_embedding_hook(hook)
        note = self.repository.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="笔记",
            content="一段笔记。",
        )
        self.assertEqual(hook.submitted, [note.id])


if __name__ == "__main__":
    unittest.main()
