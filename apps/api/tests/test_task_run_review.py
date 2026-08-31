from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.storage import Database, SqliteTaskRepository
from tests.fixtures.workspace_client import create_bound_conversation

RUN_ANSWER = "到点执行完成：本周项目进展如下，任务调度与执行链路均正常工作。"
QUESTION_ANSWER = (
    "要生成上周的项目进展总结，我需要数据来源。请提供项目进展相关的"
    "文件或列出关键信息，我就能立即为你完成总结。"
)


class ReviewProvider:
    name = "run-review"

    def __init__(self, texts=(), fail_at=None) -> None:
        self.texts = list(texts)
        self.fail_at = fail_at
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_at is not None and index == self.fail_at:
            raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, provider):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
        ),
        provider=provider,
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


class TaskRunReviewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "review.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_task(self, conversation_id: str) -> str:
        now_local = datetime.now(timezone.utc).astimezone()
        future_time = (now_local + timedelta(hours=2)).strftime("%H:%M")
        tasks = SqliteTaskRepository(Database(self.database_path))
        return tasks.create_task(
            title="每日进展总结",
            commitment="每天总结项目进展",
            schedule={"kind": "daily", "time": future_time},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        ).id

    async def _wait_for_run(self, client, task_id: str, status: str):
        for _ in range(400):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            if items and items[-1]["status"] == status:
                return items[-1]
            await asyncio.sleep(0.005)
        raise AssertionError(f"Run did not reach {status}")

    async def _run_and_wait(self, client, task_id: str, status: str):
        response = await client.post(f"/tasks/{task_id}/run")
        self.assertEqual(202, response.status_code)
        return await self._wait_for_run(client, task_id, status)

    async def test_awaiting_user_flag_and_note(self) -> None:
        provider = ReviewProvider(
            [[QUESTION_ANSWER], ['{"awaiting_user": true, "note": "需要提供进展材料"}']]
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id)

            run = await self._run_and_wait(client, task_id, "completed")
            self.assertTrue(run["awaitingUser"])
            self.assertEqual("需要提供进展材料", run["awaitingNote"])
            self.assertEqual(2, len(provider.requests))

    async def test_completed_without_awaiting(self) -> None:
        provider = ReviewProvider(
            [[RUN_ANSWER], ['{"awaiting_user": false, "note": null}']]
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id)

            run = await self._run_and_wait(client, task_id, "completed")
            self.assertFalse(run["awaitingUser"])
            self.assertIsNone(run["awaitingNote"])

    async def test_review_degrades_on_bad_json(self) -> None:
        provider = ReviewProvider([[RUN_ANSWER], ["这次执行看起来已经完成了。"]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id)

            run = await self._run_and_wait(client, task_id, "completed")
            self.assertFalse(run["awaitingUser"])
            self.assertIsNone(run["awaitingNote"])

    async def test_review_degrades_on_provider_error(self) -> None:
        provider = ReviewProvider([[RUN_ANSWER]], fail_at=1)
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id)

            run = await self._run_and_wait(client, task_id, "completed")
            self.assertFalse(run["awaitingUser"])
            self.assertEqual(2, len(provider.requests))

    async def test_failed_run_is_not_reviewed(self) -> None:
        provider = ReviewProvider([[RUN_ANSWER]], fail_at=0)
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id)

            run = await self._run_and_wait(client, task_id, "failed")
            self.assertFalse(run["awaitingUser"])
            self.assertEqual(1, len(provider.requests))


if __name__ == "__main__":
    unittest.main()