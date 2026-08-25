from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.storage import (
    Database,
    SqliteNotificationRepository,
    SqliteTaskRepository,
)
from endless_task.tasks import TaskNotificationService

RUN_ANSWER = "到点执行完成：本周项目进展如下，任务调度与执行链路均正常工作。"
QUESTION_ANSWER = "要生成总结我需要数据来源，请提供项目进展相关文件。"


class ReviewProvider:
    name = "notifications"

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
async def local_client(database_path: Path, provider, **overrides):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
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
        yield client
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


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


def _run(status: TaskRunStatus, **kwargs) -> TaskRun:
    base = dict(
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
    base.update(kwargs)
    return TaskRun(**base)


class NotificationServiceUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "n.db")
        self.database.initialize()
        self.repository = SqliteNotificationRepository(self.database)
        self.service = TaskNotificationService(
            notification_repository=self.repository
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_cancelled_run_is_silent(self) -> None:
        self.service.notify_run(_task(), _run(TaskRunStatus.CANCELLED))
        self.assertEqual((), self.repository.list_notifications())

    def test_notify_is_idempotent_per_run(self) -> None:
        run = _run(TaskRunStatus.COMPLETED)
        self.service.notify_run(_task(), run, excerpt="进展良好")
        self.service.notify_run(_task(), run, excerpt="进展良好")
        items = self.repository.list_notifications()
        self.assertEqual(1, len(items))
        self.assertEqual("run_completed", items[0].kind.value)


class TaskNotificationsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "api.db"

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

    async def test_completed_notification_lifecycle(self) -> None:
        provider = ReviewProvider(
            [[RUN_ANSWER], ['{"awaiting_user": false, "note": null}']]
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)
            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "completed")

            unread = (
                await client.get("/notifications?unread_only=true")
            ).json()["items"]
            self.assertEqual(1, len(unread))
            item = unread[0]
            self.assertEqual("run_completed", item["kind"])
            self.assertIn("本周项目进展", item["body"])
            self.assertEqual(conversation_id, item["conversationId"])

            read = (await client.post(f"/notifications/{item['id']}/read")).json()
            self.assertIsNotNone(read["notification"]["readAt"])
            unread = (
                await client.get("/notifications?unread_only=true")
            ).json()["items"]
            self.assertEqual([], unread)

    async def test_awaiting_and_failed_kinds(self) -> None:
        provider = ReviewProvider(
            [
                [QUESTION_ANSWER],
                ['{"awaiting_user": true, "note": "需要提供进展材料"}'],
            ],
            fail_at=2,
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)

            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "completed")
            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "failed")

            items = (
                await client.get("/notifications?unread_only=true")
            ).json()["items"]
            kinds = {item["kind"] for item in items}
            self.assertEqual({"run_awaiting", "run_failed"}, kinds)
            awaiting = next(i for i in items if i["kind"] == "run_awaiting")
            self.assertEqual("需要提供进展材料", awaiting["body"])
            failed = next(i for i in items if i["kind"] == "run_failed")
            self.assertTrue(failed["body"])

            count = (await client.post("/notifications/read-all")).json()["count"]
            self.assertEqual(2, count)
            unread = (
                await client.get("/notifications?unread_only=true")
            ).json()["items"]
            self.assertEqual([], unread)

    async def test_notifications_disabled(self) -> None:
        provider = ReviewProvider(
            [[RUN_ANSWER], ['{"awaiting_user": false, "note": null}']]
        )
        async with local_client(
            self.database_path, provider, notifications_enabled=False
        ) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)
            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "completed")

            items = (await client.get("/notifications")).json()["items"]
            self.assertEqual([], items)


if __name__ == "__main__":
    unittest.main()
