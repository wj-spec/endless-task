from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import TaskRunStatus, TaskRunTrigger
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks import TaskScheduler
from tests.fixtures.workspace_client import create_bound_conversation

RUN_ANSWER = "到点执行完成：本周项目进展如下，一切正常。"


class FlakyProvider:
    name = "reliability"

    def __init__(self, texts=(), fail_at=None, fail_all=False) -> None:
        self.texts = list(texts)
        self.fail_at = fail_at
        self.fail_all = fail_all
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_all or index == self.fail_at:
            raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


def utc_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@asynccontextmanager
async def local_client(database_path: Path, provider, **overrides):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            scheduler_tick_seconds=0.05,
            **overrides,
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
        yield client, app
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class TaskReliabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "rel.db"
        now_local = datetime.now(timezone.utc).astimezone()
        self.past_time = (now_local - timedelta(hours=1)).strftime("%H:%M")
        self.future_time = (now_local + timedelta(hours=2)).strftime("%H:%M")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_task(self, conversation_id: str, time: str, *, clock=None) -> str:
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        tasks = SqliteTaskRepository(Database(self.database_path), **kwargs)
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
        for _ in range(600):
            items = await self._runs(client, task_id)
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError(f"Expected {count} runs")

    async def test_sweep_converges_orphan_runs_and_notifies(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        runs = SqliteTaskRunRepository(database)
        conversation_id = SqliteChatRepository(database).create_conversation().id
        task_id = self._seed_task(conversation_id, self.future_time)
        orphan = runs.create_run(
            task_id=task_id,
            trigger=TaskRunTrigger.SCHEDULED,
            conversation_id=conversation_id,
        )

        provider = FlakyProvider([[RUN_ANSWER]])
        async with local_client(database_path=self.database_path, provider=provider) as (client, app):
            items = await self._runs(client, task_id)
            self.assertEqual(1, len(items))
            self.assertEqual("failed", items[0]["status"])
            self.assertTrue(items[0]["retryable"])
            self.assertIn("重启", items[0]["error"])
            self.assertEqual(orphan.id, items[0]["id"])

            notes = (await client.get("/notifications")).json()["items"]
            self.assertEqual(1, len(notes))
            self.assertEqual("run_failed", notes[0]["kind"])

    async def test_retry_succeeds_after_backoff(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        provider = FlakyProvider(
            [[RUN_ANSWER], ['{"awaiting_user": false, "note": null}']],
            fail_at=0,
        )
        async with local_client(
            self.database_path,
            provider,
            task_max_attempts=2,
            task_retry_backoff_seconds=0.05,
        ) as (client, _):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(
                conversation_id,
                self.past_time,
                clock=lambda: utc_iso(yesterday),
            )

            await self._wait_for_runs(client, task_id, 2)
            for _ in range(600):
                done = await self._runs(client, task_id)
                if done[1]["status"] != "running":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual("failed", done[0]["status"])
            self.assertEqual(1, done[0]["attempt"])
            self.assertEqual("completed", done[1]["status"])
            self.assertEqual(2, done[1]["attempt"])

            await asyncio.sleep(0.3)
            self.assertEqual(2, len(await self._runs(client, task_id)))

            notes = (await client.get("/notifications")).json()["items"]
            bodies = {note["kind"]: note["body"] for note in notes}
            self.assertIn("第 2 次尝试", bodies.get("run_completed", ""))

    async def test_retry_stops_at_max_attempts(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        provider = FlakyProvider([[RUN_ANSWER]], fail_all=True)
        async with local_client(
            self.database_path,
            provider,
            task_max_attempts=2,
            task_retry_backoff_seconds=0.05,
        ) as (client, _):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(
                conversation_id,
                self.past_time,
                clock=lambda: utc_iso(yesterday),
            )

            items = await self._wait_for_runs(client, task_id, 2)
            await asyncio.sleep(0.3)
            items = await self._runs(client, task_id)
            self.assertEqual(2, len(items))
            self.assertEqual({1, 2}, {item["attempt"] for item in items})

    async def test_manual_failure_is_not_retried(self) -> None:
        provider = FlakyProvider([[RUN_ANSWER]], fail_at=0)
        async with local_client(
            self.database_path,
            provider,
            task_max_attempts=3,
            task_retry_backoff_seconds=0.05,
        ) as (client, _):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, self.future_time)
            response = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(202, response.status_code)

            await self._wait_for_runs(client, task_id, 1)
            await asyncio.sleep(0.3)
            items = await self._runs(client, task_id)
            self.assertEqual(1, len(items))
            self.assertEqual("manual", items[0]["trigger"])

    def test_retry_decision_matrix(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        runs = SqliteTaskRunRepository(database)
        conversation = SqliteChatRepository(database).create_conversation()
        task_id = self._seed_task(conversation.id, self.future_time)
        scheduler = TaskScheduler(
            task_repository=SqliteTaskRepository(database),
            run_repository=runs,
            worker=None,
            retry_backoff=(timedelta(seconds=60),),
        )
        now = datetime.now(timezone.utc)
        task = SqliteTaskRepository(database).get_task(task_id)

        def seed_failed(**overrides) -> None:
            run = runs.create_run(
                task_id=task_id,
                trigger=TaskRunTrigger.SCHEDULED,
                conversation_id=conversation.id,
            )
            runs.finish_run(
                run.id, TaskRunStatus.FAILED, error="boom", **overrides
            )

        seed_failed(retryable=False, )
        self.assertIsNone(scheduler._retry_attempt(task, now + timedelta(hours=1)))

        run = runs.create_run(
            task_id=task_id,
            trigger=TaskRunTrigger.SCHEDULED,
            conversation_id=conversation.id,
        )
        runs.finish_run(run.id, TaskRunStatus.FAILED, error="boom", retryable=True)
        self.assertIsNone(
            scheduler._retry_attempt(task, now + timedelta(seconds=30))
        )
        self.assertEqual(
            2, scheduler._retry_attempt(task, now + timedelta(seconds=61))
        )

    def test_client_request_id_is_idempotent(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        chat = SqliteChatRepository(database)
        conversation = chat.create_conversation()
        first = chat.create_turn(
            conversation_id=conversation.id,
            client_request_id="taskrun:run_x",
            content="内容",
        )
        second = chat.create_turn(
            conversation_id=conversation.id,
            client_request_id="taskrun:run_x",
            content="内容",
        )
        self.assertEqual(first.turn.id, second.turn.id)


if __name__ == "__main__":
    unittest.main()