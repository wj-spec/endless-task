"""P1-2 hub 全局事件面测试（表/repo/写点/SSE 端点）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import (
    TaskRecord,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
    TaskStatus,
)
from endless_task.domain.task_schedule import parse_task_schedule
from endless_task.runtime import FakeProvider
from endless_task.storage import (
    Database,
    SqliteHubEventRepository,
    SqliteNotificationRepository,
)
from endless_task.tasks import TaskNotificationService


class HubEventRepositoryUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "hub.db")
        self.database.initialize()
        self.repository = SqliteHubEventRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_append_and_list_after_sequence(self) -> None:
        first = self.repository.append(
            "notification.created",
            conversation_id="conv-a",
            data={"kind": "run_completed", "title": "任务完成"},
        )
        second = self.repository.append(
            "notification.read",
            conversation_id="conv-b",
            data={"id": "note-1"},
        )
        self.assertEqual(1, first.event_seq)
        self.assertEqual(2, second.event_seq)
        self.assertEqual(2, self.repository.latest_seq())

        after_first = self.repository.list_after(first.event_seq)
        self.assertEqual(1, len(after_first))
        self.assertEqual("notification.read", after_first[0].event_type)
        self.assertEqual({"id": "note-1"}, after_first[0].data)

        all_events = self.repository.list_after(0)
        self.assertEqual(2, len(all_events))
        self.assertEqual(
            ["notification.created", "notification.read"],
            [event.event_type for event in all_events],
        )

    def test_append_persists_payload(self) -> None:
        event = self.repository.append(
            "notification.read_all",
            data={"count": 3},
        )
        reloaded = SqliteHubEventRepository(self.database).list_after(0)
        self.assertEqual(1, len(reloaded))
        self.assertEqual("notification.read_all", reloaded[0].event_type)
        self.assertEqual({"count": 3}, reloaded[0].data)
        self.assertEqual(event.id, reloaded[0].id)


def _task() -> TaskRecord:
    return TaskRecord(
        id="task_note",
        title="每日进展总结",
        commitment="每天总结项目进展",
        schedule=parse_task_schedule({"kind": "daily", "time": "09:00"}),
        status=TaskStatus.ACTIVE,
        source_conversation_id="conv",
        source_turn_id="turn",
        source_proposal_id=None,
        created_at="2026-08-25T00:00:00.000Z",
        updated_at="2026-08-25T00:00:00.000Z",
    )


def _run(status: TaskRunStatus) -> TaskRun:
    return TaskRun(
        id="taskrun_note",
        task_id="task_note",
        trigger=TaskRunTrigger.SCHEDULED,
        status=status,
        conversation_id="conv",
        turn_id=None,
        error=None,
        started_at="2026-08-25T01:00:00.000Z",
        finished_at="2026-08-25T01:00:01.000Z",
    )


class HubEventNotificationWriteUnitTest(unittest.TestCase):
    """通知 created 写点：TaskNotificationService 落库后写 hub 事件。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "hub.db")
        self.database.initialize()
        self.repository = SqliteNotificationRepository(self.database)
        self.hub_events = SqliteHubEventRepository(self.database)
        self.service = TaskNotificationService(
            notification_repository=self.repository,
            hub_events=self.hub_events,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_notify_run_writes_hub_event(self) -> None:
        self.service.notify_run(_task(), _run(TaskRunStatus.COMPLETED))
        events = self.hub_events.list_after(0)
        self.assertEqual(1, len(events))
        self.assertEqual("notification.created", events[0].event_type)
        self.assertEqual("conv", events[0].conversation_id)
        self.assertEqual("run_completed", events[0].data["kind"])

    def test_cancelled_run_writes_no_event(self) -> None:
        self.service.notify_run(_task(), _run(TaskRunStatus.CANCELLED))
        self.assertEqual((), self.hub_events.list_after(0))

    def test_without_hub_repo_is_backwards_compatible(self) -> None:
        plain_service = TaskNotificationService(
            notification_repository=self.repository
        )
        plain_service.notify_run(_task(), _run(TaskRunStatus.COMPLETED))
        self.assertEqual(1, len(self.repository.list_notifications()))


@asynccontextmanager
async def _client(database_path: Path):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            notifications_enabled=True,
        ),
        provider=FakeProvider(chunks=("ok",)),
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


class HubEventsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "hub_api.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_hub_events_stream_replays_after_seq(self) -> None:
        async with _client(self.database_path) as (client, app):
            container = app.state.container
            container.hub_event_repository.append(
                "notification.created",
                conversation_id="conv-x",
                data={"kind": "run_completed", "title": "后台任务完成"},
            )
            container.hub_event_repository.append(
                "notification.read",
                data={"id": "note-9"},
            )

            names: list[str] = []
            payloads: list[dict[str, object]] = []

            async def read_stream() -> None:
                async with client.stream(
                    "GET",
                    "/api/v2/hub/events",
                    params={"after_seq": 0, "idle_seconds": 0.4},
                ) as response:
                    self.assertEqual(200, response.status_code)
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            names.append(line.removeprefix("event: "))
                        elif line.startswith("data: "):
                            payloads.append(
                                json.loads(line.removeprefix("data: "))
                            )
                        if len(payloads) >= 2:
                            break

            stream_task = asyncio.create_task(read_stream())
            await asyncio.wait_for(stream_task, timeout=5)

            self.assertEqual(
                ["hub.notification.created", "hub.notification.read"], names
            )
            self.assertEqual(2, len(payloads))
            self.assertEqual(
                "conv-x", payloads[0]["conversationId"]
            )
            self.assertEqual(
                {"kind": "run_completed", "title": "后台任务完成"},
                payloads[0]["data"],
            )

    async def test_hub_events_after_seq_skips_older(self) -> None:
        async with _client(self.database_path) as (client, app):
            container = app.state.container
            container.hub_event_repository.append(
                "notification.created",
                data={"kind": "run_completed"},
            )
            second = container.hub_event_repository.append(
                "notification.created",
                data={"kind": "run_failed"},
            )

            names: list[str] = []
            async with client.stream(
                "GET",
                "/api/v2/hub/events",
                params={"after_seq": second.event_seq, "idle_seconds": 0.4},
            ) as response:
                self.assertEqual(200, response.status_code)
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        names.append(line.removeprefix("event: "))

            self.assertEqual([], names)

    async def test_read_all_emits_hub_event(self) -> None:
        async with _client(self.database_path) as (client, app):
            container = app.state.container
            # 通知 created 走 service 写点
            service = TaskNotificationService(
                notification_repository=container.notification_repository,
                hub_events=container.hub_event_repository,
            )
            service.notify_run(_task(), _run(TaskRunStatus.COMPLETED))

            response = await client.post("/notifications/read-all")
            self.assertEqual(200, response.status_code)
            self.assertEqual({"count": 1}, response.json())

            events = container.hub_event_repository.list_after(0)
            event_types = [event.event_type for event in events]
            self.assertIn("notification.created", event_types)
            self.assertIn("notification.read_all", event_types)


if __name__ == "__main__":
    unittest.main()
