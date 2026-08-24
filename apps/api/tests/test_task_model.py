from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import TaskStatus
from endless_task.domain.repositories import NotFoundError, ValidationError
from endless_task.domain.task_schedule import (
    TaskSchedule,
    TaskScheduleKind,
    parse_task_schedule,
)
from endless_task.runtime import FakeProvider
from endless_task.storage import Database, SqliteTaskRepository


class StepClock:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"2026-08-24T00:00:{self._value:02d}.000Z"


class StepIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


WEEKLY_SCHEDULE = {"kind": "weekly", "weekday": 1, "time": "09:00"}


class TaskScheduleParsingTest(unittest.TestCase):
    def test_schedule_parsing_matrix(self) -> None:
        daily = parse_task_schedule({"kind": "daily", "time": "09:00"})
        self.assertEqual(TaskScheduleKind.DAILY, daily.kind)
        self.assertEqual("09:00", daily.time)
        self.assertIsNone(daily.weekday)
        self.assertIsNone(daily.day)

        weekly = parse_task_schedule(WEEKLY_SCHEDULE)
        self.assertEqual(TaskScheduleKind.WEEKLY, weekly.kind)
        self.assertEqual(1, weekly.weekday)

        monthly = parse_task_schedule({"kind": "monthly", "day": 28, "time": "23:59"})
        self.assertEqual(TaskScheduleKind.MONTHLY, monthly.kind)
        self.assertEqual(28, monthly.day)

        parsed_from_json = parse_task_schedule('{"kind": "daily", "time": "00:00"}')
        self.assertEqual(TaskScheduleKind.DAILY, parsed_from_json.kind)

        passthrough = parse_task_schedule(daily)
        self.assertIs(daily, passthrough)

        invalid_cases = [
            {"kind": "once", "runAt": "2026-08-25T14:00:00+08:00"},
            {"kind": "hourly", "time": "09:00"},
            {"kind": "daily"},
            {"kind": "daily", "time": "9:00"},
            {"kind": "daily", "time": "24:00"},
            {"kind": "daily", "time": "09:60"},
            {"kind": "daily", "time": "09:00", "weekday": 1},
            {"kind": "weekly", "time": "09:00"},
            {"kind": "weekly", "weekday": 0, "time": "09:00"},
            {"kind": "weekly", "weekday": 8, "time": "09:00"},
            {"kind": "weekly", "weekday": True, "time": "09:00"},
            {"kind": "monthly", "time": "09:00"},
            {"kind": "monthly", "day": 0, "time": "09:00"},
            {"kind": "monthly", "day": 29, "time": "09:00"},
            {"kind": "monthly", "day": 1},
            {},
            "not-json",
            ["daily"],
        ]
        for case in invalid_cases:
            with self.assertRaises(ValidationError, msg=f"case={case!r}"):
                parse_task_schedule(case)


class SqliteTaskRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "api.db"
        )
        self.database.initialize()
        self.clock = StepClock()
        self.repository = SqliteTaskRepository(
            self.database, clock=self.clock, id_factory=StepIdFactory()
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create(self, **overrides) -> "TaskRecord":
        payload = dict(
            title="每周一进展总结",
            commitment="每周一上午 9 点总结该方向的最新进展并发送到本会话。",
            schedule=WEEKLY_SCHEDULE,
            source_conversation_id="conv_0001",
            source_turn_id="turn_0001",
        )
        payload.update(overrides)
        return self.repository.create_task(**payload)

    def test_create_task_persists(self) -> None:
        task = self._create(source_proposal_id="taskp_0001")
        self.assertEqual("task_0001", task.id)
        self.assertEqual(TaskStatus.ACTIVE, task.status)
        self.assertEqual("每周一进展总结", task.title)
        self.assertEqual(
            TaskSchedule(
                kind=TaskScheduleKind.WEEKLY, time="09:00", weekday=1
            ),
            task.schedule,
        )
        self.assertEqual("conv_0001", task.source_conversation_id)
        self.assertEqual("turn_0001", task.source_turn_id)
        self.assertEqual("taskp_0001", task.source_proposal_id)
        self.assertIsNone(task.cancelled_at)

        reloaded = self.repository.get_task(task.id)
        self.assertEqual(task, reloaded)

    def test_create_task_validation(self) -> None:
        invalid_cases = [
            dict(title="   "),
            dict(commitment=""),
            dict(title="字" * 201),
            dict(commitment="字" * 2001),
            dict(schedule={"kind": "once", "runAt": "2026-08-25T14:00:00+08:00"}),
            dict(schedule={"kind": "daily", "time": "9:00"}),
            dict(source_conversation_id=""),
            dict(source_turn_id="  "),
            dict(source_proposal_id="   "),
        ]
        for overrides in invalid_cases:
            with self.assertRaises(ValidationError, msg=f"case={overrides!r}"):
                self._create(**overrides)

    def test_get_task_and_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repository.get_task("task_missing")

    def test_list_tasks_filters(self) -> None:
        first = self._create(title="第一个")
        second = self._create(title="第二个")

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = 'cancelled', cancelled_at = ?, "
                "updated_at = ? WHERE id = ?",
                ("2026-08-24T09:00:00.000Z", "2026-08-24T09:00:00.000Z", first.id),
            )

        default = self.repository.list_tasks()
        self.assertEqual([second.id], [item.id for item in default])

        inclusive = self.repository.list_tasks(include_cancelled=True)
        self.assertEqual(
            [first.id, second.id],
            [item.id for item in inclusive],
        )
        self.assertEqual(TaskStatus.CANCELLED, inclusive[0].status)

    def test_list_tasks_ordering(self) -> None:
        first = self._create(title="第一个")
        second = self._create(title="第二个")
        self.assertEqual(
            [second.id, first.id], [item.id for item in self.repository.list_tasks()]
        )


class TaskApiTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_task_api_is_read_only(self) -> None:
        client, app = await self._client()
        container = app.state.container
        task = container.task_repository.create_task(
            title="每周一进展总结",
            commitment="每周一上午 9 点总结该方向的最新进展。",
            schedule=WEEKLY_SCHEDULE,
            source_conversation_id="conv_0001",
            source_turn_id="turn_0001",
        )
        database = container.task_repository._database

        def snapshot():
            with database.connect() as connection:
                return connection.execute(
                    "SELECT COUNT(*) AS count FROM tasks"
                ).fetchone()["count"]

        before = snapshot()

        listed = await client.get("/tasks")
        self.assertEqual(200, listed.status_code)
        items = listed.json()["items"]
        self.assertEqual([task.id], [item["id"] for item in items])
        self.assertEqual("每周一进展总结", items[0]["title"])
        self.assertEqual(
            {"kind": "weekly", "weekday": 1, "time": "09:00"}, items[0]["schedule"]
        )
        self.assertEqual("active", items[0]["status"])

        detail = await client.get(f"/tasks/{task.id}")
        self.assertEqual(200, detail.status_code)
        self.assertEqual(task.id, detail.json()["task"]["id"])

        missing = await client.get("/tasks/task_missing")
        self.assertEqual(404, missing.status_code)

        self.assertEqual(before, snapshot())


if __name__ == "__main__":
    unittest.main()
