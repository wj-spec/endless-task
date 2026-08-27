from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import Database, SqliteTaskRepository

COMMITMENT = "每周一 09:00 总结上周的项目进展"
RUN_ANSWER = (
    "收到执行请求。本周项目进展如下：完成了任务提案与确认链路，"
    "执行台账也已就位，整体进度符合预期。"
)
TASK_EXTRACTION_JSON = json.dumps(
    {
        "task": {
            "title": "每周项目进展总结",
            "commitment": COMMITMENT,
            "schedule": {"kind": "weekly", "weekday": 1, "time": "09:00"},
            "reason": "周期承诺。",
        }
    },
    ensure_ascii=False,
)


class TextProvider:
    name = "task-worker"

    def __init__(self, texts=()) -> None:
        self.texts = list(texts)
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


class BlockingProvider(TextProvider):
    def __init__(self, texts=(), event=None) -> None:
        super().__init__(texts)
        self.event = event or asyncio.Event()

    async def stream(self, request, cancellation_token):
        if not self.requests:
            await self.event.wait()
        async for item in super().stream(request, cancellation_token):
            yield item


class FailingProvider(TextProvider):
    async def stream(self, request, cancellation_token):
        if not self.requests:
            self.requests.append(request)
            raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        async for item in super().stream(request, cancellation_token):
            yield item


@asynccontextmanager
async def local_client(database_path: Path, provider):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
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


class TaskWorkerGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_task(self, conversation_id: str, **overrides) -> str:
        database = Database(self.database_path)
        tasks = SqliteTaskRepository(database)
        payload = dict(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        )
        payload.update(overrides)
        return tasks.create_task(**payload).id

    async def _wait_for_run(self, client, task_id: str, status: str):
        for _ in range(400):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            if items and items[-1]["status"] == status:
                return items[-1]
            await asyncio.sleep(0.005)
        raise AssertionError(f"Run did not reach {status}")

    async def test_manual_run_executes_in_source_conversation(self) -> None:
        provider = TextProvider([[RUN_ANSWER], [json.dumps({"task": None})]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)

            response = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(202, response.status_code)
            run = response.json()["run"]
            self.assertEqual("manual", run["trigger"])
            self.assertEqual("running", run["status"])

            finished = await self._wait_for_run(client, task_id, "completed")
            self.assertIsNotNone(finished["turnId"])

            snapshot = (
                await client.get(f"/conversations/{conversation_id}")
            ).json()
            self.assertEqual(1, len(snapshot["turns"]))
            user_content = snapshot["turns"][0]["userMessage"]["content"]
            self.assertTrue(user_content.startswith("【手动执行】"))
            self.assertIn(COMMITMENT, user_content)
            self.assertEqual("completed", snapshot["turns"][0]["turn"]["status"])

    async def test_paused_and_missing_task_are_rejected(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)
            database = Database(self.database_path)
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE tasks SET status = 'paused' WHERE id = ?",
                    (task_id,),
                )
            paused = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(409, paused.status_code)
            missing = await client.post("/tasks/task_missing/run")
            self.assertEqual(404, missing.status_code)

    async def test_concurrent_run_conflicts(self) -> None:
        event = asyncio.Event()
        provider = BlockingProvider([[RUN_ANSWER]], event=event)
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)

            first = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(202, first.status_code)
            await self._wait_for_run(client, task_id, "running")

            second = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(409, second.status_code)

            event.set()
            await self._wait_for_run(client, task_id, "completed")

    async def test_failed_turn_records_failed_run(self) -> None:
        provider = FailingProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)

            first = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(202, first.status_code)
            failed = await self._wait_for_run(client, task_id, "failed")
            self.assertTrue(failed["error"])

            tasks = (await client.get("/tasks")).json()["items"]
            self.assertEqual("active", tasks[0]["status"])

    async def test_runs_list_route(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)
            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "completed")

            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            self.assertEqual(1, len(items))
            self.assertEqual(task_id, items[0]["taskId"])
            self.assertEqual("manual", items[0]["trigger"])
            self.assertEqual("completed", items[0]["status"])
            self.assertIsNotNone(items[0]["finishedAt"])

    async def test_auto_turn_does_not_self_propose(self) -> None:
        provider = TextProvider([[RUN_ANSWER], [TASK_EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)
            await client.post(f"/tasks/{task_id}/run")
            await self._wait_for_run(client, task_id, "completed")

            for _ in range(100):
                if len(provider.requests) >= 2:
                    break
                await asyncio.sleep(0.005)
            self.assertGreaterEqual(len(provider.requests), 2)

            proposals = (
                await client.get(
                    f"/conversations/{conversation_id}/task-proposals"
                )
            ).json()["items"]
            self.assertEqual([], proposals)

    async def test_deleting_conversation_cancels_its_tasks(self) -> None:
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await client.post("/conversations")).json()["id"]
            task_id = self._seed_task(conversation_id)

            deleted = await client.delete(f"/conversations/{conversation_id}")
            self.assertEqual(204, deleted.status_code)

            tasks = (
                await client.get("/tasks?include_cancelled=true")
            ).json()["items"]
            self.assertEqual("cancelled", tasks[0]["status"])

            run = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(409, run.status_code)


if __name__ == "__main__":
    unittest.main()
