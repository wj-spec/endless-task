"""A7 生成式 UI：产物原位编辑 → 保存为新版本（版本可回溯/可回滚）。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import ArtifactKind
from endless_task.storage import Database, SqliteArtifactRepository


class _TextProvider:
    name = "a7"

    async def stream(self, request, cancellation_token):
        del request, cancellation_token
        from endless_task.runtime import ProviderCompleted, ProviderTextDelta

        yield ProviderTextDelta(text="好的。")
        yield ProviderCompleted()


@asynccontextmanager
async def _client(database_path: Path):
    app = create_app(
        settings=AppSettings(database_path=database_path),
        provider=_TextProvider(),
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


class ArtifactVersionApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "a7.db"
        database = Database(self.database_path)
        database.initialize()
        self.artifacts = SqliteArtifactRepository(database)
        self.snapshot = self.artifacts.create_artifact(
            kind=ArtifactKind.MARKDOWN,
            title="汇总报告",
            content="# 第一版\n\n内容。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.artifact_id = self.snapshot.artifact.id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_create_version_appends_and_keeps_history(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post(
                f"/artifacts/{self.artifact_id}/versions",
                json={
                    "content": "# 第二版\n\n更新后的内容。",
                    "sourceConversationId": "conv_1",
                    "sourceTurnId": "turn_2",
                    "note": "用户在界面编辑",
                },
            )
            self.assertEqual(201, response.status_code)
            payload = response.json()
            self.assertEqual(2, payload["currentVersion"]["ordinal"])
            self.assertIn("第二版", payload["currentVersion"]["content"])
            self.assertEqual("update", payload["currentVersion"]["operation"])

            versions = await client.get(
                f"/artifacts/{self.artifact_id}/versions"
            )
            items = versions.json()["items"]
            self.assertEqual(2, len(items))
            self.assertIn("第一版", items[0]["content"])
            self.assertIn("第二版", items[1]["content"])

    async def test_version_requires_source_identity(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post(
                f"/artifacts/{self.artifact_id}/versions",
                json={"content": "x", "sourceConversationId": "", "sourceTurnId": ""},
            )
        self.assertGreaterEqual(response.status_code, 400)

    async def test_unknown_artifact_returns_404(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post(
                "/artifacts/art_missing/versions",
                json={
                    "content": "x",
                    "sourceConversationId": "conv_1",
                    "sourceTurnId": "turn_1",
                },
            )
        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
