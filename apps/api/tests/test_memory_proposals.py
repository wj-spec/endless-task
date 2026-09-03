from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind, MemoryProposalStatus
from endless_task.domain.repositories import InvalidStateError, ValidationError
from endless_task.memory import MemoryProposalService
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import RunStatus
from endless_task.storage import (
    Database,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


class TextProvider:
    """Yields one scripted text completion per request."""

    name = "proposals"

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
async def local_client(database_path: Path, provider):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            knowledge_proposals_enabled=False,
            proposal_quiet_start="",
            proposal_quiet_end="",
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


class MemoryProposalRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.repository = SqliteMemoryProposalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_proposal_repository_validation(self) -> None:
        proposal = self.repository.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            reason="用户明确要求。",
        )
        self.assertEqual(proposal.status, MemoryProposalStatus.PENDING)
        self.assertIsNone(proposal.resolved_at)

        duplicate = self.repository.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_2",
            kind=MemoryKind.FACT,
            content="  用户偏好简洁回答。  ",
            reason="重复内容应复用 pending 提案。",
        )
        self.assertEqual(duplicate.id, proposal.id)

        listed = self.repository.list_proposals(conversation_id="conv_1")
        self.assertEqual([item.id for item in listed], [proposal.id])
        self.assertEqual(
            self.repository.list_proposals(conversation_id="conv_9"), ()
        )

        with self.assertRaises(ValidationError):
            self.repository.create_proposal(
                conversation_id="conv_1",
                turn_id="turn_1",
                kind="unknown",
                content="内容",
                reason="理由",
            )
        with self.assertRaises(ValidationError):
            self.repository.create_proposal(
                conversation_id="conv_1",
                turn_id="turn_1",
                kind=MemoryKind.FACT,
                content="内容",
                reason="   ",
            )
        with self.assertRaises(ValidationError):
            self.repository.create_proposal(
                conversation_id="conv_1",
                turn_id="turn_1",
                kind=MemoryKind.FACT,
                content="x" * 1001,
                reason="理由",
            )


class MemoryProposalResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteMemoryProposalRepository(self.database)
        self.memories = SqliteMemoryRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create(self, content: str = "用户偏好简洁回答。") -> str:
        return self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            kind=MemoryKind.PREFERENCE,
            content=content,
            reason="用户明确要求。",
        ).id

    def test_accept_writes_memory_and_backfills(self) -> None:
        proposal_id = self._create()
        proposal, memory = self.proposals.accept_proposal(proposal_id)

        self.assertEqual(proposal.status, MemoryProposalStatus.ACCEPTED)
        self.assertEqual(proposal.resolved_memory_id, memory.id)
        self.assertIsNotNone(proposal.resolved_at)
        self.assertEqual(memory.content, "用户偏好简洁回答。")
        self.assertEqual(memory.write_origin, "confirmed_proposal")
        self.assertEqual(memory.source_conversation_id, "conv_1")
        self.assertEqual(memory.source_turn_id, "turn_1")
        self.assertEqual([record.id for record in self.memories.list_memories()], [memory.id])

        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal_id)
        with self.assertRaises(InvalidStateError):
            self.proposals.reject_proposal(proposal_id)

    def test_reject_path_writes_no_memory(self) -> None:
        proposal_id = self._create()
        proposal = self.proposals.reject_proposal(proposal_id)
        self.assertEqual(proposal.status, MemoryProposalStatus.REJECTED)
        self.assertIsNone(proposal.resolved_memory_id)
        self.assertEqual(self.memories.list_memories(), ())
        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal_id)

    def test_resolve_missing_proposal_raises_not_found(self) -> None:
        from endless_task.domain.repositories import NotFoundError

        with self.assertRaises(NotFoundError):
            self.proposals.accept_proposal("memp_missing")
        with self.assertRaises(NotFoundError):
            self.proposals.reject_proposal("memp_missing")


class MemoryProposalServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteMemoryProposalRepository(self.database)
        self.memories = SqliteMemoryRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider) -> MemoryProposalService:
        return MemoryProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            memory_repository=self.memories,
            model="test-model",
        )

    async def test_duplicate_and_limit_rules(self) -> None:
        payload = {
            "proposals": [
                {"kind": "preference", "content": "用户偏好中文回答。", "reason": "明确表达"},
                {"kind": "fact", "content": "用户在上海工作。", "reason": "明确表达"},
                {"kind": "fact", "content": "用户养了一只猫。", "reason": "超出上限"},
            ]
        }
        provider = TextProvider([json.dumps(payload, ensure_ascii=False)])
        service = self._service(provider)

        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="我以后都用中文提问。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 2)
        self.assertEqual(self.memories.list_memories(), ())

        repeated = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_2",
            user_message="记住：重复内容不应新增提案。",
            assistant_message="好的。",
        )
        self.assertEqual(len(repeated), 2)
        pending = self.proposals.list_proposals(conversation_id="conv_1")
        self.assertEqual(len(pending), 2)
        self.assertEqual(
            {item.id for item in pending},
            {item.id for item in repeated},
        )

    async def test_marker_gate_skips_model_call(self) -> None:
        provider = TextProvider([json.dumps({"proposals": []}, ensure_ascii=False)])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="随便聊聊。",
            assistant_message="好的。",
        )
        self.assertEqual(created, ())
        self.assertEqual(len(provider.requests), 0)

    async def test_marker_gate_disabled_still_extracts(self) -> None:
        payload = {
            "proposals": [
                {"kind": "fact", "content": "用户在上海工作。", "reason": "明确表达"}
            ]
        }
        provider = TextProvider([json.dumps(payload, ensure_ascii=False)])
        service = MemoryProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            memory_repository=self.memories,
            model="test-model",
            marker_gate_enabled=False,
        )
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="随便聊聊。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 1)

    def test_silence_prompt_rules(self) -> None:
        from endless_task.memory.proposal_service import _EXTRACTION_SYSTEM_PROMPT

        self.assertIn("领域规范", _EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("宁可漏记", _EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("知识通道", _EXTRACTION_SYSTEM_PROMPT)

    async def test_garbage_extraction_is_ignored(self) -> None:
        provider = TextProvider(["这根本不是 JSON"])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住我喜欢猫。",
            assistant_message="好的。",
        )
        self.assertEqual(created, ())

    async def test_extraction_request_includes_awareness_list(self) -> None:
        pending = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_0",
            kind=MemoryKind.PREFERENCE,
            content="用户偏好本地优先的方案。",
            reason="明确表达",
        )
        self.proposals.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_0",
            kind=MemoryKind.FACT,
            content="用户喜欢深色模式。",
            reason="明确表达",
        )
        self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户在上海工作。",
            source_conversation_id="conv_1",
            source_turn_id="turn_0",
        )
        provider = TextProvider([json.dumps({"proposals": []})])
        service = self._service(provider)
        await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住我喜欢猫。",
            assistant_message="好的。",
        )
        transcript = provider.requests[0].messages[1].content
        self.assertIn("与已记住语义相同的不要再提案", transcript)
        self.assertIn(
            f"待确认 {pending.id}：用户偏好本地优先的方案。", transcript
        )
        self.assertIn("已记住：用户在上海工作。", transcript)
        self.assertNotIn("用户喜欢深色模式。", transcript)

        empty_database = Database(
            Path(self._temporary_directory.name) / "empty.db"
        )
        empty_database.initialize()
        empty_provider = TextProvider([json.dumps({"proposals": []})])
        empty_service = MemoryProposalService(
            provider=empty_provider,
            proposal_repository=SqliteMemoryProposalRepository(empty_database),
            memory_repository=SqliteMemoryRepository(empty_database),
            model="test-model",
        )
        await empty_service.generate_for_turn(
            conversation_id="conv_9",
            turn_id="turn_9",
            user_message="记住我喜欢猫。",
            assistant_message="好的。",
        )
        self.assertNotIn(
            "语义相同的不要再提案",
            empty_provider.requests[0].messages[1].content,
        )


class MemoryProposalGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(container, run_id: str):
        return await wait_for_run_terminal(container, run_id)

    async def _wait_for_proposals(self, client, conversation_id: str, count: int):
        for _ in range(200):
            response = await client.get(
                f"/conversations/{conversation_id}/memory-proposals"
            )
            items = response.json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Proposals did not appear in time")

    async def test_completed_turn_generates_pending_proposal_without_memory_write(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {
                        "kind": "preference",
                        "content": "用户偏好本地优先的方案。",
                        "reason": "用户在对话中明确表达。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的，我了解你的偏好了。", extraction])
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "请记住我喜欢本地优先的方案。",
                idempotency_key="request-1",
            )
            run_id = handle["runId"]
            status = await self._wait_for_terminal(container, run_id)
            self.assertIs(status, RunStatus.COMPLETED)

            items = await self._wait_for_proposals(client, conversation["id"], 1)
            self.assertEqual(items[0]["content"], "用户偏好本地优先的方案。")
            self.assertEqual(items[0]["status"], "pending")
            self.assertEqual(items[0]["turnId"], run_id)

            self.assertEqual(len(provider.requests), 2)
            extraction_request = provider.requests[1]
            transcript = extraction_request.messages[-1].content
            self.assertIn("请记住我喜欢本地优先的方案。", transcript)
            self.assertEqual(extraction_request.temperature, 0.0)

        database = Database(self.database_path)
        with database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM memories").fetchone()
        self.assertEqual(row["count"], 0)

    async def test_extraction_failure_does_not_affect_turn(self) -> None:
        provider = TextProvider(
            ["好的。", "不应到达"], fail_from_index=1
        )
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "记住我喜欢猫。",
                idempotency_key="request-1",
            )
            status = await self._wait_for_terminal(container, handle["runId"])
            self.assertIs(status, RunStatus.COMPLETED)

            await asyncio.sleep(0.05)
            response = await client.get(
                f"/conversations/{conversation['id']}/memory-proposals"
            )
            self.assertEqual(response.json()["items"], [])


    async def test_resolve_via_api_accept_then_conflict(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {
                        "kind": "preference",
                        "content": "用户偏好本地优先的方案。",
                        "reason": "用户在对话中明确表达。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的。", extraction])
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "请记住我喜欢本地优先的方案。",
                idempotency_key="request-1",
            )
            await self._wait_for_terminal(container, handle["runId"])
            items = await self._wait_for_proposals(client, conversation["id"], 1)
            proposal_id = items[0]["id"]

            response = await client.post(
                f"/memory-proposals/{proposal_id}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["proposal"]["status"], "accepted")
            self.assertEqual(
                payload["proposal"]["resolvedMemoryId"], payload["memory"]["id"]
            )
            self.assertEqual(payload["memory"]["writeOrigin"], "confirmed_proposal")

            response = await client.post(
                f"/memory-proposals/{proposal_id}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 409)

            response = await client.get(
                f"/conversations/{conversation['id']}/memory-proposals",
                params={"include_resolved": True},
            )
            self.assertEqual(response.json()["items"][0]["status"], "accepted")

        database = Database(self.database_path)
        with database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM memories").fetchone()
        self.assertEqual(row["count"], 1)

    async def test_resolve_via_api_reject_writes_no_memory(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {"kind": "fact", "content": "用户在上海工作。", "reason": "明确表达。"}
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的。", extraction])
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "我在上海工作。",
                idempotency_key="request-1",
            )
            await self._wait_for_terminal(container, handle["runId"])
            items = await self._wait_for_proposals(client, conversation["id"], 1)

            response = await client.post(
                f"/memory-proposals/{items[0]['id']}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["proposal"]["status"], "rejected")
            self.assertNotIn("memory", response.json())

            response = await client.post(
                f"/memory-proposals/{items[0]['id']}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(response.status_code, 409)

        database = Database(self.database_path)
        with database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM memories").fetchone()
        self.assertEqual(row["count"], 0)


if __name__ == "__main__":
    unittest.main()