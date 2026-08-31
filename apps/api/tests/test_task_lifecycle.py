from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import TaskRecord, TaskStatus
from endless_task.domain.task_schedule import parse_task_schedule
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks import TaskScheduler
from tests.fixtures.workspace_client import create_bound_conversation


class TextProvider:
    name = "task-lifecycle"

    def __init__(self, texts=("好的。",)) -> None:
        self.texts = list(texts)

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        for chunk in self.texts:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, **settings_overrides):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            scheduler_tick_seconds=0.05,
            **settings_overrides,
        ),
        provider=TextProvider(),
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


class TaskLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "life.db"
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

    def _set_status(self, task_id: str, status: str) -> None:
        database = Database(self.database_path)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

    async def _task_ids(self, client, include_cancelled=False):
        suffix = "?include_cancelled=true" if include_cancelled else ""
        response = await client.get(f"/tasks{suffix}")
        return [item["id"] for item in response.json()["items"]]

    async def test_transition_matrix_and_visibility(self) -> None:
        async with local_client(self.database_path) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, self.future_time)

            missing = await client.post("/tasks/task_missing/pause")
            self.assertEqual(404, missing.status_code)

            paused = await client.post(f"/tasks/{task_id}/pause")
            self.assertEqual(200, paused.status_code)
            self.assertEqual("paused", paused.json()["task"]["status"])

            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/pause")).status_code)
            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/run")).status_code)

            resumed = await client.post(f"/tasks/{task_id}/resume")
            self.assertEqual(200, resumed.status_code)
            self.assertEqual("active", resumed.json()["task"]["status"])
            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/resume")).status_code)

            self._set_status(task_id, "paused")
            cancelled = await client.post(f"/tasks/{task_id}/cancel")
            self.assertEqual(200, cancelled.status_code)
            self.assertEqual("cancelled", cancelled.json()["task"]["status"])

            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/cancel")).status_code)
            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/pause")).status_code)
            self.assertEqual(409, (await client.post(f"/tasks/{task_id}/resume")).status_code)

            self.assertNotIn(task_id, await self._task_ids(client))
            self.assertIn(task_id, await self._task_ids(client, True))

    async def test_resume_after_pause_does_not_catch_up(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        Database(self.database_path).initialize()
        task_id = self._seed_task(
            "conv_seed",
            self.past_time,
            clock=lambda: utc_iso(yesterday),
        )
        self._set_status(task_id, "paused")

        async with local_client(self.database_path) as client:
            resumed = await client.post(f"/tasks/{task_id}/resume")
            self.assertEqual(200, resumed.status_code)

            await asyncio.sleep(0.3)
            runs = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            self.assertEqual([], runs)

    def test_is_due_anchor_respects_resumed_at(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        scheduler = TaskScheduler(
            task_repository=SqliteTaskRepository(database),
            run_repository=SqliteTaskRunRepository(database),
            worker=None,
        )
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        schedule = parse_task_schedule({"kind": "daily", "time": self.past_time})

        def record(resumed_at) -> TaskRecord:
            return TaskRecord(
                id="task_anchor",
                title="t",
                commitment="c",
                schedule=schedule,
                status=TaskStatus.ACTIVE,
                source_conversation_id="conv",
                source_turn_id="turn",
                source_proposal_id=None,
                created_at=utc_iso(yesterday),
                updated_at=utc_iso(yesterday),
                resumed_at=resumed_at,
            )

        self.assertTrue(scheduler.is_due(record(None), now))
        self.assertFalse(scheduler.is_due(record(utc_iso(now)), now))


if __name__ == "__main__":
    unittest.main()