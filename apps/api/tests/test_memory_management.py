from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import Database, SqliteMemoryRepository


class TextProvider:
    name = "management"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request, cancellation_token):
        self.requests.append(request)
        yield ProviderTextDelta(text="好的。")
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, provider):
    app = create_app(settings=AppSettings(database_path=database_path), provider=provider)
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


class MemoryManagementTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "manage.db"
        database = Database(self.database_path)
        database.initialize()
        self.memories = SqliteMemoryRepository(database)
        self.kept = self.memories.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.removed = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户已删除的记忆。",
            source_conversation_id="conv_1",
            source_turn_id="turn_2",
        )
        self.memories.delete_memory(self.removed.id)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_list_update_delete_memories_via_api(self) -> None:
        async with local_client(self.database_path, TextProvider()) as client:
            response = await client.get("/memories")
            items = response.json()["items"]
            self.assertEqual([item["id"] for item in items], [self.kept.id])
            self.assertEqual(items[0]["writeOrigin"], "confirmed_proposal")
            self.assertEqual(items[0]["sourceConversationId"], "conv_1")

            response = await client.get("/memories", params={"include_deleted": True})
            self.assertEqual(len(response.json()["items"]), 2)

            response = await client.patch(
                f"/memories/{self.kept.id}", json={"content": "用户偏好更简洁的回答。"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["memory"]["content"], "用户偏好更简洁的回答。"
            )

            response = await client.delete(f"/memories/{self.kept.id}")
            self.assertEqual(response.status_code, 204)

            response = await client.get("/memories")
            self.assertEqual(response.json()["items"], [])

            response = await client.get("/memories", params={"include_deleted": True})
            statuses = {
                item["id"]: item["status"] for item in response.json()["items"]
            }
            self.assertEqual(statuses[self.kept.id], "deleted")

    async def test_management_error_semantics(self) -> None:
        async with local_client(self.database_path, TextProvider()) as client:
            response = await client.patch("/memories/mem_missing", json={"content": "x"})
            self.assertEqual(response.status_code, 404)
            response = await client.delete("/memories/mem_missing")
            self.assertEqual(response.status_code, 404)

            response = await client.patch(
                f"/memories/{self.removed.id}", json={"content": "x"}
            )
            self.assertEqual(response.status_code, 409)
            response = await client.delete(f"/memories/{self.removed.id}")
            self.assertEqual(response.status_code, 409)

            response = await client.patch(
                f"/memories/{self.kept.id}", json={"content": "x", "extra": 1}
            )
            self.assertEqual(response.status_code, 400)
            response = await client.patch(
                f"/memories/{self.kept.id}", json={"content": "   "}
            )
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
