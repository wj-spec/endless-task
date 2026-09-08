from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import FeedbackRating
from endless_task.runtime import FakeProvider
from endless_task.storage import Database, SqliteResponseFeedbackRepository


class ResponseFeedbackApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._database_path = (
            Path(self._temporary_directory.name) / "feedback-api.db"
        )
        self._clients: list[tuple[httpx.AsyncClient, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(self) -> httpx.AsyncClient:
        app = create_app(
            settings=AppSettings(
                database_path=self._database_path,
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
            ),
            provider=FakeProvider(chunks=("你好",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._clients.append((client, lifespan))
        return client

    def _repo(self) -> SqliteResponseFeedbackRepository:
        database = Database(self._database_path)
        database.initialize()
        return SqliteResponseFeedbackRepository(database)

    async def test_record_feedback_endpoint(self) -> None:
        client = await self._client()
        response = await client.post(
            "/turns/run1/feedback",
            json={"rating": "down", "reason": "太啰嗦", "conversationId": "c1"},
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("down", body["rating"])
        self.assertEqual("太啰嗦", body["reason"])
        self.assertEqual("run1", body["turnId"])

    async def test_record_feedback_is_idempotent(self) -> None:
        client = await self._client()
        first = await client.post(
            "/turns/run1/feedback", json={"rating": "down", "variantId": "v1"}
        )
        second = await client.post(
            "/turns/run1/feedback", json={"rating": "up", "variantId": "v1"}
        )
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual("up", second.json()["rating"])
        self.assertEqual(first.json()["id"], second.json()["id"])
        # 只有一条
        self.assertEqual(1, len(self._repo().list_for_conversation("")))

    async def test_record_feedback_rejects_invalid_rating(self) -> None:
        client = await self._client()
        response = await client.post(
            "/turns/run1/feedback", json={"rating": "meh"}
        )
        self.assertEqual(422, response.status_code)

    async def test_record_feedback_without_conversation(self) -> None:
        client = await self._client()
        response = await client.post(
            "/turns/run1/feedback", json={"rating": "up"}
        )
        self.assertEqual(200, response.status_code)
        record = self._repo().get_for_turn("run1")
        self.assertIsNotNone(record)
        self.assertEqual(FeedbackRating.UP, record.rating)


if __name__ == "__main__":
    unittest.main()
