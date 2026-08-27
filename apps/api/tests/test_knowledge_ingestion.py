"""R5.7 摄入健壮性：API 侧文件解析矩阵与 /knowledge-sources/import。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.knowledge.ingestion import (
    MAX_EXTRACTED_CHARS,
    IngestionError,
    ingest_file_bytes,
    split_extension,
)
from endless_task.runtime import FakeProvider


@asynccontextmanager
async def local_client(database_path: Path):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            knowledge_proposals_enabled=False,
        ),
        provider=FakeProvider(),
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class IngestionMatrixTest(unittest.TestCase):
    def test_utf8_file(self):
        ingested = ingest_file_bytes("咖啡机规范".encode("utf-8"), file_name="a.txt")
        self.assertEqual(ingested.text, "咖啡机规范")
        self.assertEqual(ingested.encoding, "utf-8")
        self.assertFalse(ingested.truncated)
        self.assertEqual(ingested.size, len("咖啡机规范".encode("utf-8")))
        self.assertEqual(len(ingested.sha256), 64)

    def test_gb18030_fallback(self):
        raw = "报销流程规范".encode("gb18030")
        ingested = ingest_file_bytes(raw, file_name="a.txt")
        self.assertEqual(ingested.text, "报销流程规范")
        self.assertEqual(ingested.encoding, "gb18030")

    def test_binary_rejected(self):
        raw = b"\x00\x01\x02" * 100
        with self.assertRaises(IngestionError) as captured:
            ingest_file_bytes(raw, file_name="a.txt")
        self.assertEqual(captured.exception.code, "binary_file")

    def test_empty_rejected(self):
        with self.assertRaises(IngestionError) as captured:
            ingest_file_bytes(b"", file_name="a.txt")
        self.assertEqual(captured.exception.code, "empty_file")

    def test_whitespace_only_rejected(self):
        with self.assertRaises(IngestionError) as captured:
            ingest_file_bytes(b"   \n\t  ", file_name="a.txt")
        self.assertEqual(captured.exception.code, "empty_file")

    def test_oversize_rejected(self):
        raw = b"a" * (5 * 1000 * 1000 + 1)
        with self.assertRaises(IngestionError) as captured:
            ingest_file_bytes(raw, file_name="a.txt")
        self.assertEqual(captured.exception.code, "file_too_large")

    def test_extension_whitelist(self):
        with self.assertRaises(IngestionError) as captured:
            ingest_file_bytes(b"content", file_name="a.docx")
        self.assertEqual(captured.exception.code, "unsupported_format")

    def test_allowed_extensions(self):
        for name in ("a.txt", "a.md", "a.csv", "a.json", "a.log", "a.MARKDOWN"):
            ingested = ingest_file_bytes(b"content", file_name=name)
            self.assertEqual(ingested.text, "content")

    def test_long_text_truncated(self):
        raw = ("行" * (MAX_EXTRACTED_CHARS + 100)).encode("utf-8")
        ingested = ingest_file_bytes(raw, file_name="a.txt")
        self.assertTrue(ingested.truncated)
        self.assertEqual(len(ingested.text), MAX_EXTRACTED_CHARS)

    def test_split_extension(self):
        self.assertEqual(split_extension("notes.MD"), "md")
        self.assertEqual(split_extension("noext"), "")


class ImportEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self._dir.name) / "api.db"

    def tearDown(self):
        self._dir.cleanup()

    async def test_import_creates_source_with_provenance(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/knowledge-sources/import",
                files={"file": ("规范.txt", "奶管必须清洗".encode("utf-8"))},
                data={"title": "咖啡机规范"},
            )
            self.assertEqual(response.status_code, 201)
            payload = response.json()
            source = payload["source"]
            self.assertEqual(source["title"], "咖啡机规范")
            self.assertEqual(source["kind"], "file")
            self.assertEqual(source["fileName"], "规范.txt")
            self.assertEqual(source["content"], "奶管必须清洗")
            self.assertGreater(source["fileSize"], 0)
            self.assertEqual(len(source["fileSha256"]), 64)
            self.assertFalse(payload["truncated"])
            self.assertEqual(payload["encoding"], "utf-8")

    async def test_import_defaults_title_to_filename(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/knowledge-sources/import",
                files={"file": (" Weekly.log", b"line one")},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["source"]["title"], "Weekly.log")

    async def test_import_rejects_bad_format_with_message(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/knowledge-sources/import",
                files={"file": ("report.pdf", b"%PDF-1.4 fake")},
            )
            self.assertEqual(response.status_code, 400)
            error = response.json()["error"]
            self.assertEqual(error["code"], "unsupported_format")
            self.assertIn("格式", error["message"])

    async def test_import_rejects_undecodable_file(self):
        async with local_client(self.database_path) as client:
            response = await client.post(
                "/knowledge-sources/import",
                files={"file": ("bad.txt", b"\xff\xfe\xfdgarbage\xff\xfe")},
            )
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
