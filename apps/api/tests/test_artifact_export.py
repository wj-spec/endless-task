from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.artifacts.export_service import (
    ExportError,
    content_disposition_header,
    export_filename,
)
from endless_task.domain.models import ArtifactKind, ArtifactRecord, ArtifactStatus
from endless_task.runtime import FakeProvider

try:
    import reportlab  # noqa: F401

    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

PATHOLOGICAL_CONTENT = (
    "# 安全测试 <script>alert(1)</script>\n\n"
    "包含 **粗体**、*斜体* 与 `行内代码` 的段落，以及 & 符号与 <标签>。\n"
    "这一行特别长。" + "长" * 300 + "\n\n"
    "## 表格\n\n"
    "| 名称 | 数量 |\n"
    "| --- | --- |\n"
    "| 苹果 | 3 |\n"
    "| 梨 | 5 |\n\n"
    "- 列表项 <b>伪标记</b>\n"
    "- 第二项 & 更多\n\n"
    "```python\nprint('<不是标记> & 内容')\n```\n\n"
    "> 引用块带 <尖括号>\n"
)


class ArtifactExportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "api.db",
                memory_proposals_enabled=False,
                artifact_proposals_enabled=False,
            ),
            provider=FakeProvider(chunks=("你好",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        return client, app

    def _seed(self, app, *, title="发布计划", kind=ArtifactKind.MARKDOWN, content=None):
        container = app.state.container
        snapshot = container.artifact_repository.create_artifact(
            title=title,
            kind=kind,
            content=PATHOLOGICAL_CONTENT if content is None else content,
            source_conversation_id="conv_src",
            source_turn_id="turn_src",
        )
        return snapshot.artifact

    async def test_markdown_export_is_verbatim(self) -> None:
        client, app = await self._client()
        artifact = self._seed(app, content="# 原始内容\n\n逐字导出。")

        response = await client.get(f"/artifacts/{artifact.id}/export")
        self.assertEqual(200, response.status_code)
        self.assertEqual("text/markdown; charset=utf-8", response.headers["content-type"])
        self.assertEqual("# 原始内容\n\n逐字导出。", response.text)
        disposition = response.headers["content-disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertNotEqual(0, len(response.content))
        self.assertFalse(response.content.startswith(b"\xef\xbb\xbf"))

        text_artifact = self._seed(
            app, title="纯文本", kind=ArtifactKind.TEXT, content="纯文本内容"
        )
        text_response = await client.get(f"/artifacts/{text_artifact.id}/export")
        self.assertEqual("text/plain; charset=utf-8", text_response.headers["content-type"])
        self.assertIn("txt", text_response.headers["content-disposition"])

    async def test_html_export_is_self_contained(self) -> None:
        client, app = await self._client()
        artifact = self._seed(app)

        response = await client.get(
            f"/artifacts/{artifact.id}/export", params={"format": "html"}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("text/html; charset=utf-8", response.headers["content-type"])
        html = response.text
        self.assertIn("<html", html)
        self.assertIn("<table>", html)
        self.assertIn("苹果", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn(".html", response.headers["content-disposition"])

    @unittest.skipUnless(HAS_REPORTLAB, "reportlab is not installed")
    async def test_pdf_export_structure_and_escaping(self) -> None:
        client, app = await self._client()
        artifact = self._seed(app)

        response = await client.get(
            f"/artifacts/{artifact.id}/export", params={"format": "pdf"}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/pdf", response.headers["content-type"])
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 1_000)
        self.assertIn(".pdf", response.headers["content-disposition"])

    async def test_export_error_matrix(self) -> None:
        client, app = await self._client()

        missing = await client.get("/artifacts/art_missing/export")
        self.assertEqual(404, missing.status_code)

        artifact = self._seed(app)
        invalid_format = await client.get(
            f"/artifacts/{artifact.id}/export", params={"format": "docx"}
        )
        self.assertEqual(400, invalid_format.status_code)
        self.assertEqual(
            "invalid_request", invalid_format.json()["error"]["code"]
        )

        container = app.state.container
        container.artifact_repository.delete_artifact(artifact.id)
        deleted = await client.get(f"/artifacts/{artifact.id}/export")
        self.assertEqual(200, deleted.status_code)

    async def test_pdf_unavailable_degrades(self) -> None:
        client, app = await self._client()
        artifact = self._seed(app)

        def raise_unavailable():
            raise ExportError(
                "export_unavailable", "PDF 导出依赖缺失。", status_code=503
            )

        with mock.patch(
            "endless_task.artifacts.export_service.load_pdf_dependencies",
            side_effect=raise_unavailable,
        ):
            response = await client.get(
                f"/artifacts/{artifact.id}/export", params={"format": "pdf"}
            )
        self.assertEqual(503, response.status_code)
        self.assertEqual("export_unavailable", response.json()["error"]["code"])

        markdown = await client.get(f"/artifacts/{artifact.id}/export")
        self.assertEqual(200, markdown.status_code)

    async def test_export_is_read_only(self) -> None:
        client, app = await self._client()
        artifact = self._seed(app)
        container = app.state.container

        before = container.artifact_repository.get_artifact(artifact.id)
        versions_before = container.artifact_repository.list_versions(artifact.id)

        await client.get(f"/artifacts/{artifact.id}/export")
        await client.get(f"/artifacts/{artifact.id}/export", params={"format": "html"})
        if HAS_REPORTLAB:
            await client.get(
                f"/artifacts/{artifact.id}/export", params={"format": "pdf"}
            )

        after = container.artifact_repository.get_artifact(artifact.id)
        versions_after = container.artifact_repository.list_versions(artifact.id)
        self.assertEqual(before, after)
        self.assertEqual(versions_before, versions_after)


class ExportFilenameTest(unittest.TestCase):
    def _record(self, *, title: str, kind=ArtifactKind.MARKDOWN) -> ArtifactRecord:
        return ArtifactRecord(
            id="art_0001",
            title=title,
            kind=kind,
            status=ArtifactStatus.ACTIVE,
            current_version_ordinal=2,
            created_at="2026-08-24T00:00:00.000Z",
            updated_at="2026-08-24T00:00:01.000Z",
        )

    def test_export_filename_sanitization(self) -> None:
        self.assertEqual(
            "发布计划-v2.md", export_filename(self._record(title="发布计划"), fmt="markdown")
        )
        self.assertEqual(
            "纯文本-v2.txt",
            export_filename(
                self._record(title="纯文本", kind=ArtifactKind.TEXT), fmt="markdown"
            ),
        )
        self.assertEqual(
            "发布计划-v2.html", export_filename(self._record(title="发布计划"), fmt="html")
        )
        self.assertEqual(
            "发布计划-v2.pdf", export_filename(self._record(title="发布计划"), fmt="pdf")
        )

        dangerous = export_filename(
            self._record(title="a/b\\c:d*e?f\"g<h>i|j"), fmt="markdown"
        )
        self.assertEqual("a_b_c_d_e_f_g_h_i_j-v2.md", dangerous)

        collapsed = export_filename(self._record(title="多   空格\t标题"), fmt="markdown")
        self.assertEqual("多 空格 标题-v2.md", collapsed)

        long_title = export_filename(self._record(title="字" * 200), fmt="markdown")
        stem = long_title.rsplit("-v", 1)[0]
        self.assertLessEqual(len(stem), 80)

        fallback = export_filename(self._record(title="///"), fmt="markdown")
        self.assertEqual("artifact-art_0001-v2.md", fallback)

        with self.assertRaises(ExportError):
            export_filename(self._record(title="标题"), fmt="docx")

    def test_content_disposition_header(self) -> None:
        header = content_disposition_header("发布计划-v2.md")
        self.assertIn("attachment", header)
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%E5%8F%91", header)

        ascii_header = content_disposition_header("中文版.md")
        self.assertIn('filename="endless-task-export.md"', ascii_header)


if __name__ == "__main__":
    unittest.main()
