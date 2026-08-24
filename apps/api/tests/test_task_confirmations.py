from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.api.serialization import task_json, task_proposal_json
from endless_task.domain.repositories import InvalidStateError, NotFoundError
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteTaskProposalRepository,
    SqliteTaskRepository,
)

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

    name = "task-confirmations"

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


@asynccontextmanager
async def local_client(
    database_path: Path, provider, *, task_proposals_enabled=True
):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
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


class TaskConfirmationRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteTaskProposalRepository(self.database)
        self.tasks = SqliteTaskRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_proposal(self):
        return self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            title="每周项目进展总结",
            commitment=PERIODIC_COMMITMENT,
            schedule=WEEKLY_SCHEDULE,
            reason="用户要求周期性执行",
        )

    def test_accept_writes_task_and_resolves_proposal(self) -> None:
        proposal = self._create_proposal()
        accepted, task = self.proposals.accept_proposal(proposal.id)

        self.assertEqual("accepted", accepted.status.value)
        self.assertEqual(task.id, accepted.resolved_task_id)
        self.assertIsNotNone(accepted.resolved_at)

        self.assertEqual("active", task.status.value)
        self.assertEqual("每周项目进展总结", task.title)
        self.assertEqual(PERIODIC_COMMITMENT, task.commitment)
        self.assertEqual(proposal.id, task.source_proposal_id)
        self.assertEqual("conv_1", task.source_conversation_id)
        self.assertEqual("turn_1", task.source_turn_id)
        self.assertEqual(1, len(self.tasks.list_tasks()))

    def test_reject_writes_no_task(self) -> None:
        proposal = self._create_proposal()
        rejected = self.proposals.reject_proposal(proposal.id)
        self.assertEqual("rejected", rejected.status.value)
        self.assertIsNotNone(rejected.resolved_at)
        self.assertIsNone(rejected.resolved_task_id)
        self.assertEqual(0, len(self.tasks.list_tasks(include_cancelled=True)))

    def test_resolve_twice_conflicts_and_missing_404(self) -> None:
        proposal = self._create_proposal()
        self.proposals.accept_proposal(proposal.id)
        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal.id)
        with self.assertRaises(InvalidStateError):
            self.proposals.reject_proposal(proposal.id)
        with self.assertRaises(NotFoundError):
            self.proposals.accept_proposal("taskp_missing")

    def test_schedule_description_in_serialization(self) -> None:
        proposal = self._create_proposal()
        payload = task_proposal_json(proposal)
        self.assertEqual("每周一 09:00", payload["scheduleDescription"])

        _, task = self.proposals.accept_proposal(proposal.id)
        task_payload = task_json(task)
        self.assertEqual("每周一 09:00", task_payload["scheduleDescription"])


class TaskConfirmationGateTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_end_to_end_proposal_to_task(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER], [TASK_EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": PERIODIC_USER},
            )
            turn_id = created.json()["turnId"]
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])

            items = await self._wait_for_task_proposals(
                client, conversation_id, 1
            )
            proposal_id = items[0]["id"]

            resolved = await client.post(
                f"/task-proposals/{proposal_id}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, resolved.status_code)
            payload = resolved.json()
            self.assertEqual("accepted", payload["proposal"]["status"])
            self.assertEqual(
                payload["proposal"]["resolvedTaskId"], payload["task"]["id"]
            )

            tasks = (await client.get("/tasks")).json()["items"]
            self.assertEqual(1, len(tasks))
            self.assertEqual(PERIODIC_COMMITMENT, tasks[0]["commitment"])
            self.assertEqual("每周一 09:00", tasks[0]["scheduleDescription"])
            self.assertEqual(proposal_id, tasks[0]["sourceProposalId"])

            listed = await client.get(
                f"/conversations/{conversation_id}/task-proposals"
            )
            self.assertEqual([], listed.json()["items"])
            listed_resolved = await client.get(
                f"/conversations/{conversation_id}/task-proposals"
                "?include_resolved=true"
            )
            self.assertEqual(
                "accepted", listed_resolved.json()["items"][0]["status"]
            )

    async def test_reject_via_api_writes_no_task(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER], [TASK_EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": PERIODIC_USER},
            )
            await self._wait_for_terminal(client, created.json()["turnId"])
            items = await self._wait_for_task_proposals(
                client, conversation_id, 1
            )
            resolved = await client.post(
                f"/task-proposals/{items[0]['id']}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(200, resolved.status_code)
            self.assertEqual("rejected", resolved.json()["proposal"]["status"])
            self.assertEqual([], (await client.get("/tasks")).json()["items"])

    async def test_resolve_missing_returns_404_and_twice_409(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER], [TASK_EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            missing = await client.post(
                "/task-proposals/taskp_missing/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(404, missing.status_code)

            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": PERIODIC_USER},
            )
            await self._wait_for_terminal(client, created.json()["turnId"])
            items = await self._wait_for_task_proposals(
                client, conversation_id, 1
            )
            first = await client.post(
                f"/task-proposals/{items[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, first.status_code)
            second = await client.post(
                f"/task-proposals/{items[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(409, second.status_code)

    async def test_confirmation_clause_follows_flag(self) -> None:
        provider = TextProvider([[PERIODIC_ANSWER]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": "你好"},
            )
            await self._wait_for_terminal(client, created.json()["turnId"])
        system_content = provider.requests[0].messages[0].content
        self.assertIn("以用户在提案卡片上的确认为准", system_content)

        provider_off = TextProvider([[PERIODIC_ANSWER]])
        async with local_client(
            self.database_path, provider_off, task_proposals_enabled=False
        ) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": "你好"},
            )
            await self._wait_for_terminal(client, created.json()["turnId"])
        self.assertNotIn(
            "以用户在提案卡片上的确认为准",
            provider_off.requests[0].messages[0].content,
        )


if __name__ == "__main__":
    unittest.main()
