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
    KnowledgeProposalStatus,
    KnowledgeProposalType,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from endless_task.knowledge import KnowledgeProposalService
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.storage import (
    Database,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
)
from tests.fixtures.workspace_client import create_bound_conversation


class TextProvider:
    """Yields one scripted text completion per request."""

    name = "knowledge-proposals"

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
                runtime="v1",
                memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
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


class KnowledgeProposalRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteKnowledgeProposalRepository(self.database)
        self.sources = SqliteKnowledgeRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _source(self, title: str = "咖啡机规范", content: str = "使用咖啡机后必须清洗奶管。") -> str:
        return self.sources.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
        ).id

    def test_add_proposal_validation_and_dedup(self) -> None:
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.ADD_SOURCE,
            payload={"title": "品牌视觉规范", "content": "主色为深绿。", "reason": "长期复用"},
        )
        self.assertEqual(proposal.status, KnowledgeProposalStatus.PENDING)
        self.assertEqual(proposal.payload["kind"], "note")

        duplicate = self.proposals.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_2",
            proposal_type=KnowledgeProposalType.ADD_SOURCE,
            payload={"title": "其他标题", "content": "  主色为深绿。  "},
        )
        self.assertEqual(duplicate.id, proposal.id)

        with self.assertRaises(ValidationError):
            self.proposals.create_proposal(
                conversation_id="conv_1",
                turn_id="turn_1",
                proposal_type=KnowledgeProposalType.ADD_SOURCE,
                payload={"title": "", "content": "内容"},
            )
        with self.assertRaises(ValidationError):
            self.proposals.create_proposal(
                conversation_id="conv_1",
                turn_id="turn_1",
                proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
                payload={"reason": "缺少 source_id"},
            )

    def test_expire_proposal_dedup_per_source(self) -> None:
        source_id = self._source()
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source_id},
        )
        duplicate = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_2",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source_id},
        )
        self.assertEqual(duplicate.id, proposal.id)

    def test_accept_add_creates_agent_source_with_provenance(self) -> None:
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.ADD_SOURCE,
            payload={"title": "品牌视觉规范", "content": "主色为深绿。"},
        )
        accepted, source = self.proposals.accept_proposal(proposal.id)
        self.assertEqual(accepted.status, KnowledgeProposalStatus.ACCEPTED)
        self.assertEqual(accepted.resolved_source_id, source.id)
        self.assertEqual(source.origin, KnowledgeSourceOrigin.AGENT)
        self.assertEqual(source.kind, KnowledgeSourceKind.NOTE)
        self.assertEqual(source.source_conversation_id, "conv_1")
        self.assertEqual(source.proposed_by_turn_id, "turn_1")
        self.assertEqual(source.status, KnowledgeSourceStatus.ACTIVE)

        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal.id)

    def test_accept_expire_expires_active_source(self) -> None:
        source_id = self._source()
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source_id},
        )
        accepted, source = self.proposals.accept_proposal(proposal.id)
        self.assertEqual(accepted.status, KnowledgeProposalStatus.ACCEPTED)
        self.assertEqual(source.id, source_id)
        self.assertEqual(source.status, KnowledgeSourceStatus.EXPIRED)

    def test_accept_expire_requires_active_source(self) -> None:
        source_id = self._source()
        self.sources.expire_source(source_id)
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.EXPIRE_SOURCE,
            payload={"source_id": source_id},
        )
        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal.id)

    def test_reject_writes_no_source(self) -> None:
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            proposal_type=KnowledgeProposalType.ADD_SOURCE,
            payload={"title": "标题", "content": "内容"},
        )
        rejected = self.proposals.reject_proposal(proposal.id)
        self.assertEqual(rejected.status, KnowledgeProposalStatus.REJECTED)
        self.assertEqual(self.sources.list_sources(), [])
        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal.id)

    def test_missing_proposal_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.proposals.accept_proposal("knp_missing")
        with self.assertRaises(NotFoundError):
            self.proposals.reject_proposal("knp_missing")


class KnowledgeProposalServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteKnowledgeProposalRepository(self.database)
        self.sources = SqliteKnowledgeRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider) -> KnowledgeProposalService:
        return KnowledgeProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            knowledge_repository=self.sources,
            model="test-model",
        )

    async def test_add_proposal_created_and_limited(self) -> None:
        payload = {
            "proposals": [
                {"type": "add_source", "title": "A", "content": "内容一", "reason": "r"},
                {"type": "add_source", "title": "B", "content": "内容二", "reason": "r"},
                {"type": "add_source", "title": "C", "content": "内容三", "reason": "超出上限"},
                {"type": "bogus", "content": "忽略"},
            ]
        }
        provider = TextProvider([json.dumps(payload, ensure_ascii=False)])
        created = await self._service(provider).generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="规范讨论，请记录。",
            assistant_message="好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。",
        )
        self.assertEqual(len(created), 2)
        self.assertEqual(self.sources.list_sources(), [])

    async def test_add_skips_content_matching_active_source(self) -> None:
        self.sources.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="已有",
            content="主色为深绿。",
        )
        payload = {
            "proposals": [
                {"type": "add_source", "title": "重复", "content": "主色为深绿。"},
            ]
        }
        provider = TextProvider([json.dumps(payload, ensure_ascii=False)])
        created = await self._service(provider).generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="随便聊聊一下。",
            assistant_message="好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。",
        )
        self.assertEqual(created, ())

    async def test_expire_requires_active_source_id(self) -> None:
        source_id = self.sources.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="有效",
            content="仍然有效。",
        ).id
        payload = {
            "proposals": [
                {"type": "expire_source", "source_id": source_id, "reason": "用户说已作废"},
                {"type": "expire_source", "source_id": "ks_unknown", "reason": "不存在"},
            ]
        }
        provider = TextProvider([json.dumps(payload, ensure_ascii=False)])
        created = await self._service(provider).generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="那个规范作废了。",
            assistant_message="好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].proposal_type, KnowledgeProposalType.EXPIRE_SOURCE)
        self.assertEqual(created[0].payload["source_id"], source_id)
        self.assertEqual(created[0].payload["title"], "有效")

    async def test_awareness_block_lists_pending_and_active(self) -> None:
        self.sources.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title="咖啡机规范",
            content="使用咖啡机后必须清洗奶管。",
        )
        provider = TextProvider([json.dumps({"proposals": []})])
        await self._service(provider).generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="随便聊聊一下。",
            assistant_message="好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。",
        )
        transcript = provider.requests[0].messages[1].content
        self.assertIn("咖啡机规范", transcript)
        self.assertIn("expire_source 的 source_id 必须取自活跃知识 id", transcript)

    async def test_garbage_extraction_is_ignored(self) -> None:
        provider = TextProvider(["这根本不是 JSON"])
        created = await self._service(provider).generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="随便聊聊一下。",
            assistant_message="好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。",
        )
        self.assertEqual(created, ())


class KnowledgeProposalGateTest(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    async def _wait_for_proposals(client, conversation_id: str, count: int):
        for _ in range(200):
            response = await client.get(
                f"/conversations/{conversation_id}/knowledge-proposals"
            )
            items = response.json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Proposals did not appear in time")

    async def _complete_turn(self, client, conversation_id: str, key: str, content: str):
        response = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": key},
            json={"content": content},
        )
        self.assertEqual(response.status_code, 202)
        turn_id = response.json()["turnId"]
        payload = await self._wait_for_terminal(client, turn_id)
        self.assertEqual(payload["turnStatus"], "completed")
        return turn_id

    async def test_completed_turn_generates_proposal_and_resolve_flow(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {
                        "type": "add_source",
                        "title": "咖啡机规范",
                        "content": "使用咖啡机后必须清洗奶管，否则奶路会堵塞。",
                        "reason": "用户提供的长期规范。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。", extraction])
        async with local_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            turn_id = await self._complete_turn(
                client, conversation["id"], "request-1", "咖啡机用完必须清洗奶管，记一下。"
            )

            items = await self._wait_for_proposals(client, conversation["id"], 1)
            self.assertEqual(items[0]["type"], "add_source")
            self.assertEqual(items[0]["status"], "pending")
            self.assertEqual(items[0]["turnId"], turn_id)

            pending = (await client.get("/proposals/pending")).json()["items"]
            self.assertTrue(
                any(
                    item["kind"] == "knowledge" and item["id"] == items[0]["id"]
                    for item in pending
                )
            )

            response = await client.post(
                f"/knowledge-proposals/{items[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 200)
            source = response.json()["source"]
            self.assertEqual(source["origin"], "agent")

            listed = (
                await client.get("/knowledge-sources?status=active")
            ).json()["items"]
            self.assertEqual([item["id"] for item in listed], [source["id"]])

    async def test_reject_creates_no_source(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {"type": "add_source", "title": "T", "content": "C", "reason": "r"}
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。", extraction])
        async with local_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            await self._complete_turn(client, conversation["id"], "request-1", "聊聊。")
            items = await self._wait_for_proposals(client, conversation["id"], 1)
            response = await client.post(
                f"/knowledge-proposals/{items[0]['id']}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["proposal"]["status"], "rejected")
            listed = (await client.get("/knowledge-sources?status=active")).json()
            self.assertEqual(listed["items"], [])

    async def test_ephemeral_conversation_skips_extraction(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {"type": "add_source", "title": "T", "content": "C", "reason": "r"}
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。", extraction, "好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。", extraction])
        async with local_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            turn_id = await self._complete_turn(
                client, conversation["id"], "request-1", "聊聊。"
            )
            branch = (
                await client.post(
                    f"/conversations/{conversation['id']}/branches",
                    json={"forkTurnId": turn_id},
                )
            ).json()["conversation"]
            self.assertEqual(branch["kind"], "ephemeral")
            await self._complete_turn(client, branch["id"], "request-2", "开小差。")
            await asyncio.sleep(0.05)
            response = await client.get(
                f"/conversations/{branch['id']}/knowledge-proposals"
            )
            self.assertEqual(response.json()["items"], [])

    async def test_extraction_failure_does_not_affect_turn(self) -> None:
        provider = TextProvider(["好的，我已经完整了解这条规范的具体内容，也明白了它背后的原因，之后遇到相关问题时我会参考它来回答你。", "不应到达"], fail_from_index=1)
        async with local_client(self.database_path, provider) as client:
            conversation = await create_bound_conversation(client)
            await self._complete_turn(client, conversation["id"], "request-1", "聊聊。")
            await asyncio.sleep(0.05)
            response = await client.get(
                f"/conversations/{conversation['id']}/knowledge-proposals"
            )
            self.assertEqual(response.json()["items"], [])