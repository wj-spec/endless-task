from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import Database, SqliteTaskRepository
from tests.fixtures.v2_client import run_snapshot
from tests.fixtures.workspace_client import create_bound_conversation

RUN_ANSWER = (
    "到点执行完成：本周项目进展如下，任务调度与执行链路均正常工作，"
    "没有新的风险需要上报。"
)


class TextProvider:
    name = "task-scheduler"

    def __init__(self, texts=(), fail_first=False) -> None:
        self.texts = list(texts)
        self.fail_first = fail_first
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_first and index == 0:
            raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, provider, **settings_overrides):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            scheduler_tick_seconds=0.05,
            **settings_overrides,
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


def utc_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskSchedulerGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"
        now_local = datetime.now(timezone.utc).astimezone()
        self.past_time = (now_local - timedelta(hours=1)).strftime("%H:%M")
        self.future_time = (now_local + timedelta(hours=2)).strftime("%H:%M")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_task(self, conversation_id: str, time: str, *, clock=None) -> str:
        database = Database(self.database_path)
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        tasks = SqliteTaskRepository(database, **kwargs)
        return tasks.create_task(
            title="每日进展总结",
            commitment="每天总结项目进展",
            schedule={"kind": "daily", "time": time},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        ).id

    async def _runs(self, client, task_id: str):
        return (await client.get(f"/tasks/{task_id}/runs")).json()["items"]

    async def _wait_for_runs(self, client, task_id: str, count: int):
        for _ in range(400):
            items = await self._runs(client, task_id)
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Scheduled run did not appear")

    async def _wait_for_run(self, client, task_id: str, status: str):
        for _ in range(400):
            items = await self._runs(client, task_id)
            if items and items[-1]["status"] == status:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError(f"Run did not reach {status}")

    async def test_due_task_runs_on_schedule_once(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        provider = TextProvider([[RUN_ANSWER], [json.dumps({"task": None})]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(
                conversation_id,
                self.past_time,
                clock=lambda: utc_iso(yesterday),
            )

            items = await self._wait_for_run(client, task_id, "completed")
            self.assertEqual("scheduled", items[0]["trigger"])

            await asyncio.sleep(0.3)
            items = await self._runs(client, task_id)
            self.assertEqual(1, len(items))
            self.assertEqual("completed", items[0]["status"])

            snapshot = await run_snapshot(client, conversation_id)
            user_messages = [
                entry["data"]["content"]
                for entry in snapshot["entries"]
                if entry.get("actor") == "user"
                and isinstance(entry.get("data", {}).get("content"), str)
            ]
            self.assertTrue(
                any(msg.startswith("【到点执行】") for msg in user_messages)
            )

    async def test_not_due_task_does_not_run(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, self.future_time)
            await asyncio.sleep(0.3)
            self.assertEqual([], await self._runs(client, task_id))

    async def test_catch_up_once_and_no_retry_after_failure(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        provider = TextProvider([[RUN_ANSWER]], fail_first=True)
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(
                conversation_id,
                self.past_time,
                clock=lambda: utc_iso(yesterday),
            )

            items = await self._wait_for_run(client, task_id, "failed")

            await asyncio.sleep(0.3)
            items = await self._runs(client, task_id)
            self.assertEqual(1, len(items))

    async def test_paused_task_is_not_scheduled(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, self.past_time)
            database = Database(self.database_path)
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE tasks SET status = 'paused' WHERE id = ?",
                    (task_id,),
                )
            await asyncio.sleep(0.3)
            self.assertEqual([], await self._runs(client, task_id))

    async def test_scheduler_disabled(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(
            self.database_path, provider, scheduler_enabled=False
        ) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, self.past_time)
            await asyncio.sleep(0.3)
            self.assertEqual([], await self._runs(client, task_id))


if __name__ == "__main__":
    unittest.main()