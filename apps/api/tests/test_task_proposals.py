from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.repositories import NotFoundError, ValidationError
from endless_task.domain.task_schedule import (
    TaskScheduleKind,
    describe_task_schedule,
    parse_task_schedule,
)
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteTaskProposalRepository,
    SqliteTaskRepository,
)
from endless_task.tasks import TaskProposalService

PERIODIC_USER = "以后每周一上午九点帮我总结上周的项目进展"
PERIODIC_ANSWER = (
    "好的，我记下了这个安排：每周一 09:00 我会为你总结上周的项目进展。"
    "该安排会在你确认后才会生效。"
)
PERIODIC_COMMITMENT = "每周一 09:00 总结上周的项目进展"
WEEKLY_SCHEDULE = {"kind": "weekly", "weekday": 1, "time": "09:00"}

TASK_EXTRACTION_JSON = json.dumps(
    {
        "task": {
            "title": "每周项目进展总结",
            "commitment": PERIODIC_COMMITMENT,
            "schedule": WEEKLY_SCHEDULE,
            "reason": "用户明确要求每周重复，Assistant 复述并承担了承诺。",
        }
    },
    ensure_ascii=False,
)


class TextProvider:
    """Yields one scripted text completion per request."""

    name = "task-proposals"

    def __init__(self, texts=(), fail_from_index=None) -> None:
        self.texts = list(texts)
        self.fail_from_index = fail_from_index
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_from_index is not None and index >= self.fail_from_index:
            raise ProviderError("provider_down", "提取服务不可用", retryable=True)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(
    database_path: Path, provider, *, task_proposals_enabled=True
):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=task_proposals_enabled,
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


class TaskScheduleDescriptionTest(unittest.TestCase):
    def test_describe_schedules(self) -> None:
        daily = parse_task_schedule({"kind": "daily", "time": "09:00"})
        weekly = parse_task_schedule(WEEKLY_SCHEDULE)
        monthly = parse_task_schedule({"kind": "monthly", "day": 3, "time": "18:30"})
        self.assertEqual("每天 09:00", describe_task_schedule(daily))
        self.assertEqual("每周一 09:00", describe_task_schedule(weekly))
        self.assertEqual("每月 3 日 18:30", describe_task_schedule(monthly))


class TaskProposalRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteTaskProposalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create(self, **overrides):
        payload = dict(
            conversation_id="conv_1",
            turn_id="turn_1",
            title="每周项目进展总结",
            commitment=PERIODIC_COMMITMENT,
            schedule=WEEKLY_SCHEDULE,
            reason="用户要求周期性执行",
        )
        payload.update(overrides)
        return self.proposals.create_proposal(**payload)

    def test_validation_and_dedup(self) -> None:
        proposal = self._create()
        self.assertEqual("pending", proposal.status.value)
        self.assertEqual(TaskScheduleKind.WEEKLY, proposal.schedule.kind)
        self.assertEqual(1, proposal.schedule.weekday)
        self.assertEqual("09:00", proposal.schedule.time)

        duplicate = self._create(turn_id="turn_2")
        self.assertNotEqual(proposal.id, duplicate.id)
        self.assertEqual(
            2, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )
        cancelled = self.proposals.cancel_proposal(proposal.id)
        self.assertEqual("cancelled", cancelled.status.value)
        self.assertIsNotNone(cancelled.resolved_at)
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )

        other_schedule = self._create(
            schedule={"kind": "daily", "time": "09:00"}
        )
        self.assertNotEqual(proposal.id, other_schedule.id)

        with self.assertRaises(ValidationError):
            self._create(title="   ")
        with self.assertRaises(ValidationError):
            self._create(commitment="   ")
        with self.assertRaises(ValidationError):
            self._create(schedule={"kind": "once", "time": "09:00"})
        with self.assertRaises(NotFoundError):
            self.proposals.get_proposal("taskp_missing")

    def test_list_filters_by_conversation_and_status(self) -> None:
        self._create()
        self._create(
            conversation_id="conv_2",
            commitment="每天早上 08:00 提醒我喝水",
            schedule={"kind": "daily", "time": "08:00"},
        )
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_2"))
        )
        self.assertEqual(
            0, len(self.proposals.list_proposals(conversation_id="conv_3"))
        )


class TaskProposalServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteTaskProposalRepository(self.database)
        self.tasks = SqliteTaskRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider) -> TaskProposalService:
        return TaskProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            model="test-model",
            task_repository=self.tasks,
        )

    async def test_periodic_commitment_generates_proposal(self) -> None:
        provider = TextProvider([TASK_EXTRACTION_JSON])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual(1, len(created))
        proposal = created[0]
        self.assertEqual("conv_1", proposal.conversation_id)
        self.assertEqual("turn_1", proposal.turn_id)
        self.assertEqual("每周项目进展总结", proposal.title)
        self.assertEqual(PERIODIC_COMMITMENT, proposal.commitment)
        self.assertEqual("pending", proposal.status.value)
        self.assertIsNone(proposal.resolved_task_id)

    async def test_one_off_and_invalid_schedule_are_skipped(self) -> None:
        payloads = (
            json.dumps({"task": None}),
            json.dumps(
                {
                    "task": {
                        "title": "整理文档",
                        "commitment": "明天下午整理这份文档",
                        "schedule": {"kind": "once", "time": "15:00"},
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "task": {
                        "title": "每天提醒",
                        "commitment": "每天提醒我喝水",
                        "schedule": {"kind": "daily", "time": "九点"},
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "task": {
                        "title": "",
                        "commitment": PERIODIC_COMMITMENT,
                        "schedule": WEEKLY_SCHEDULE,
                    }
                },
                ensure_ascii=False,
            ),
        )
        for payload in payloads:
            provider = TextProvider([payload])
            service = self._service(provider)
            created = await service.generate_for_turn(
                conversation_id="conv_1",
                turn_id="turn_1",
                user_message=PERIODIC_USER,
                assistant_message=PERIODIC_ANSWER,
            )
            self.assertEqual((), created, payload)
        self.assertEqual(
            0, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )

    async def test_deduplication_against_proposals_and_tasks(self) -> None:
        provider = TextProvider([TASK_EXTRACTION_JSON])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual(1, len(created))

        repeated = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_2",
            user_message="再说一次。",
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual(1, len(repeated))
        self.assertEqual(
            "cancelled",
            self.proposals.get_proposal(created[0].id).status.value,
        )
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )
        self.assertEqual(
            repeated[0].id,
            self.proposals.list_proposals(conversation_id="conv_1")[0].id,
        )

        cross = await service.generate_for_turn(
            conversation_id="conv_2",
            turn_id="turn_9",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual(1, len(cross))
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_2"))
        )
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )

        self.tasks.create_task(
            title="每周项目进展总结",
            commitment=PERIODIC_COMMITMENT,
            schedule=WEEKLY_SCHEDULE,
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        fresh_proposals = SqliteTaskProposalRepository(self.database)
        service_with_task = TaskProposalService(
            provider=TextProvider([TASK_EXTRACTION_JSON]),
            proposal_repository=fresh_proposals,
            model="test-model",
            task_repository=self.tasks,
        )
        blocked = await service_with_task.generate_for_turn(
            conversation_id="conv_2",
            turn_id="turn_3",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual((), blocked)
        self.assertEqual(
            1, len(fresh_proposals.list_proposals(conversation_id="conv_2"))
        )

    async def test_short_answers_are_skipped(self) -> None:
        provider = TextProvider([TASK_EXTRACTION_JSON])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message=PERIODIC_USER,
            assistant_message="好的，每周一见。",
        )
        self.assertEqual((), created)
        self.assertEqual([], provider.requests)

    async def test_missing_reason_uses_default(self) -> None:
        payload = json.dumps(
            {
                "task": {
                    "title": "每周项目进展总结",
                    "commitment": PERIODIC_COMMITMENT,
                    "schedule": WEEKLY_SCHEDULE,
                }
            },
            ensure_ascii=False,
        )
        service = self._service(TextProvider([payload]))
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertEqual(1, len(created))
        self.assertEqual("用户要求周期性执行", created[0].reason)

    async def test_extraction_request_includes_awareness_list(self) -> None:
        self.tasks.create_task(
            title="每周进展总结",
            commitment="每周一 09:00 总结上周的项目进展",
            schedule=WEEKLY_SCHEDULE,
            source_conversation_id="conv_1",
            source_turn_id="turn_0",
        )
        pending = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_0",
            title="每日天气",
            commitment="每天 08:00 播报天气",
            schedule={"kind": "daily", "time": "08:00"},
            reason="用户要求周期性执行",
        )
        self.proposals.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_0",
            title="每日吃药",
            commitment="每天 07:00 提醒吃药",
            schedule={"kind": "daily", "time": "07:00"},
            reason="用户要求周期性执行",
        )
        provider = TextProvider([json.dumps({"task": None})])
        service = self._service(provider)
        await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        transcript = provider.requests[0].messages[1].content
        self.assertIn("与“已安排”语义相同的输出 null", transcript)
        self.assertIn(
            "已安排：每周一 09:00 总结上周的项目进展（每周一 09:00）",
            transcript,
        )
        self.assertIn(f"待确认 {pending.id}：每天 08:00 播报天气", transcript)
        self.assertNotIn("每天 07:00 提醒吃药", transcript)

        empty_database = Database(
            Path(self._temporary_directory.name) / "empty.db"
        )
        empty_database.initialize()
        empty_provider = TextProvider([json.dumps({"task": None})])
        empty_service = TaskProposalService(
            provider=empty_provider,
            proposal_repository=SqliteTaskProposalRepository(empty_database),
            model="test-model",
            task_repository=SqliteTaskRepository(empty_database),
        )
        await empty_service.generate_for_turn(
            conversation_id="conv_9",
            turn_id="turn_9",
            user_message=PERIODIC_USER,
            assistant_message=PERIODIC_ANSWER,
        )
        self.assertNotIn(
            "语义相同的不要再提案",
            empty_provider.requests[0].messages[1].content,
        )


class TaskProposalGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(client: httpx.AsyncClient, turn_id: str):
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            payload = response.json()
            if payload["turnStatus"] in {"completed", "failed", "cancelled"}:
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("Turn did not reach a terminal state")

    async def _wait_for_task_proposals(self, client, conversation_id, count):
        for _ in range(200):
            response = await client.get(
                f"/conversations/{conversation_id}/task-proposals"
            )
            items = response.json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Task proposals did not appear in time")

    @staticmethod
    async def _run_turn(client: httpx.AsyncClient, content: str) -> tuple:
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "request-1"},
            json={"content": content},
        )
        return conversation_id, created.json()["turnId"]

    async def test_end_to_end_periodic_proposal_and_list_route(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER], [TASK_EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(
                client, PERIODIC_USER
            )
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])

            items = await self._wait_for_task_proposals(
                client, conversation_id, 1
            )
            proposal = items[0]
            self.assertEqual("每周项目进展总结", proposal["title"])
            self.assertEqual(PERIODIC_COMMITMENT, proposal["commitment"])
            self.assertEqual("pending", proposal["status"])
            self.assertEqual(WEEKLY_SCHEDULE, proposal["schedule"])
            self.assertEqual(conversation_id, proposal["conversationId"])
            self.assertEqual(turn_id, proposal["turnId"])
            self.assertIsNone(proposal["resolvedTaskId"])

    async def test_provider_failure_keeps_turn_completed(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER]], fail_from_index=1)
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(
                client, PERIODIC_USER
            )
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])
            response = await client.get(
                f"/conversations/{conversation_id}/task-proposals"
            )
            self.assertEqual([], response.json()["items"])

    async def test_system_prompt_clause_follows_flag(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER]])
        async with local_client(self.database_path, provider) as (client, app):
            _, turn_id = await self._run_turn(client, PERIODIC_USER)
            await self._wait_for_terminal(client, turn_id)
        system_content = provider.requests[0].messages[0].content
        self.assertIn("确认后才生效", system_content)
        self.assertIn("用户提出一次性定时事项时", system_content)
        self.assertNotIn("一次性定时安排能力尚未就绪", system_content)

        provider_off = TextProvider([[PERIODIC_ANSWER]])
        async with local_client(
            self.database_path, provider_off, task_proposals_enabled=False
        ) as (client, app):
            _, turn_id = await self._run_turn(client, PERIODIC_USER)
            await self._wait_for_terminal(client, turn_id)
        system_content = provider_off.requests[0].messages[0].content
        self.assertNotIn("确认后才生效", system_content)

    async def test_task_list_in_context(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        tasks = SqliteTaskRepository(database)
        tasks.create_task(
            title="每周进展总结",
            commitment="每周一 09:00 汇总上周进展并生成完整的总结报告发送给用户",
            schedule=WEEKLY_SCHEDULE,
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )

        provider = TextProvider([[PERIODIC_ANSWER]])
        async with local_client(self.database_path, provider) as (client, app):
            _, turn_id = await self._run_turn(client, "你好")
            await self._wait_for_terminal(client, turn_id)

        system_content = provider.requests[0].messages[0].content
        self.assertIn("用户已确认的安排", system_content)
        self.assertIn("《每周进展总结》", system_content)
        self.assertIn("每周一 09:00", system_content)
        self.assertNotIn("汇总上周进展并生成完整的总结报告", system_content)


if __name__ == "__main__":
    unittest.main()
