from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.artifacts import ArtifactProposalService
from endless_task.domain.models import (
    ArtifactKind,
    ArtifactProposalStatus,
    ArtifactStatus,
    ArtifactVersionOperation,
)
from endless_task.domain.repositories import InvalidStateError, NotFoundError, ValidationError
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
)

LONG_ANSWER = (
    "# 发布计划\n\n"
    "## 目标\n本季度完成 Endless Task 的公开发布，覆盖桌面端与本地 API，"
    "并确保本地优先的数据纪律在所有平台上保持一致。\n\n"
    "## 里程碑\n1. 功能冻结：完成 P3 Artifact Runtime 的全部验收，"
    "包括版本管理、来源引用与导出能力。\n"
    "2. 内测：邀请二十名用户进行为期两周的内测，按主题收集反馈并每周汇总。\n"
    "3. 发布：同步发布安装器、使用文档与版本说明，并在发布后三天内跟进首批反馈。\n\n"
    "## 风险与对策\n- 内测反馈集中：提前准备问题分类与响应流程，明确修复优先级。\n"
    "- 文档缺口：发布前一周冻结文档变更并组织校对，确保新手引导完整。\n"
    "- 平台差异：在发布前完成 macOS 与 Linux 的回归测试清单。\n\n"
    "## 沟通计划\n- 每周向内测用户发布一次进展摘要与已知问题列表。\n"
    "- 发布当天同步公告、更新日志与常见问题解答。\n\n"
    "## 验收标准\n- 全部发布门槛测试通过，且无未关闭的阻断问题。\n"
    "- 新手引导完成率在内测样本中不低于八成。\n"
)

EXTRACTION_JSON = json.dumps(
    {
        "proposal": {
            "title": "发布计划",
            "kind": "markdown",
            "content": LONG_ANSWER,
            "reason": "完整的发布计划文档，值得整体保留。",
        }
    },
    ensure_ascii=False,
)


class TextProvider:
    """Yields one scripted text completion per request."""

    name = "artifact-proposals"

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
async def local_client(database_path: Path, provider, *, artifact_proposals_enabled=True):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=artifact_proposals_enabled,
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


class ArtifactProposalRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteArtifactProposalRepository(self.database)
        self.artifacts = SqliteArtifactRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create(self, **overrides) -> str:
        payload = dict(
            conversation_id="conv_1",
            turn_id="turn_1",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=LONG_ANSWER,
            reason="值得整体保留。",
        )
        payload.update(overrides)
        return self.proposals.create_proposal(**payload).id

    def test_validation_and_dedup(self) -> None:
        proposal_id = self._create()
        proposal = self.proposals.get_proposal(proposal_id)
        self.assertEqual(ArtifactProposalStatus.PENDING, proposal.status)

        duplicate = self.proposals.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_2",
            title="另一份计划",
            kind=ArtifactKind.MARKDOWN,
            content=f"  {LONG_ANSWER.strip()}  ",
            reason="重复内容应复用 pending 提案。",
        )
        self.assertEqual(proposal_id, duplicate.id)

        listed = self.proposals.list_proposals(conversation_id="conv_1")
        self.assertEqual([proposal_id], [item.id for item in listed])

        with self.assertRaises(ValidationError):
            self._create(title="   ", content="不同的内容")
        with self.assertRaises(ValidationError):
            self._create(title="字" * 201, content="不同的内容")
        with self.assertRaises(ValidationError):
            self._create(content="   ")
        with self.assertRaises(ValidationError):
            self._create(kind="pdf", content="不同的内容")
        with self.assertRaises(ValidationError):
            self._create(reason="字" * 201, content="不同的内容")

    def test_accept_creates_artifact_version_one(self) -> None:
        proposal_id = self._create()
        proposal, artifact = self.proposals.accept_proposal(proposal_id)

        self.assertEqual(ArtifactProposalStatus.ACCEPTED, proposal.status)
        self.assertEqual(artifact.id, proposal.resolved_artifact_id)
        self.assertIsNotNone(proposal.resolved_at)
        self.assertEqual("发布计划", artifact.title)
        self.assertEqual(ArtifactKind.MARKDOWN, artifact.kind)
        self.assertEqual(ArtifactStatus.ACTIVE, artifact.status)
        self.assertEqual(1, artifact.current_version_ordinal)

        version = self.artifacts.get_version(artifact.id, 1)
        self.assertEqual(ArtifactVersionOperation.CREATE, version.operation)
        self.assertEqual(LONG_ANSWER.strip(), version.content)
        self.assertEqual("conv_1", version.source_conversation_id)
        self.assertEqual("turn_1", version.source_turn_id)
        self.assertEqual((), version.source_labels)

        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal_id)
        with self.assertRaises(InvalidStateError):
            self.proposals.reject_proposal(proposal_id)

    def test_reject_path_writes_no_artifact(self) -> None:
        proposal_id = self._create()
        proposal = self.proposals.reject_proposal(proposal_id)
        self.assertEqual(ArtifactProposalStatus.REJECTED, proposal.status)
        self.assertIsNone(proposal.resolved_artifact_id)
        self.assertEqual((), self.artifacts.list_artifacts())
        with self.assertRaises(InvalidStateError):
            self.proposals.accept_proposal(proposal_id)

    def test_resolve_missing_proposal_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.proposals.accept_proposal("artp_missing")
        with self.assertRaises(NotFoundError):
            self.proposals.reject_proposal("artp_missing")


class ArtifactProposalServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteArtifactProposalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider) -> ArtifactProposalService:
        return ArtifactProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            model="test-model",
        )

    async def test_duplicate_pending_content_is_skipped(self) -> None:
        provider = TextProvider([EXTRACTION_JSON])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="帮我整理发布计划。",
            assistant_message=LONG_ANSWER,
        )
        self.assertEqual(1, len(created))

        repeated = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_2",
            user_message="再来一遍。",
            assistant_message=LONG_ANSWER,
        )
        self.assertEqual((), repeated)
        self.assertEqual(
            1, len(self.proposals.list_proposals(conversation_id="conv_1"))
        )

    async def test_invalid_payloads_are_ignored(self) -> None:
        for payload in (
            "这根本不是 JSON",
            json.dumps({"proposal": None}),
            json.dumps({"proposal": {"title": "", "kind": "markdown", "content": "内容"}}),
            json.dumps({"proposal": {"title": "标题", "kind": "pdf", "content": "内容"}}),
            json.dumps({"proposal": {"title": "标题", "kind": "markdown", "content": "  "}}),
        ):
            provider = TextProvider([payload])
            service = self._service(provider)
            created = await service.generate_for_turn(
                conversation_id="conv_1",
                turn_id="turn_1",
                user_message="随便聊聊。",
                assistant_message="长" * 500,
            )
            self.assertEqual((), created, payload)


class ArtifactProposalGateTest(unittest.IsolatedAsyncioTestCase):
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

    async def _wait_for_proposals(self, client, conversation_id: str, count: int):
        for _ in range(200):
            response = await client.get(
                f"/conversations/{conversation_id}/artifact-proposals"
            )
            items = response.json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Proposals did not appear in time")

    @staticmethod
    async def _run_turn(client: httpx.AsyncClient) -> str:
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "request-1"},
            json={"content": "帮我整理发布计划"},
        )
        return conversation_id, created.json()["turnId"]

    async def test_proposal_generation_does_not_write_artifacts(self) -> None:
        provider = TextProvider([[LONG_ANSWER], [EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(client)
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])

            items = await self._wait_for_proposals(client, conversation_id, 1)
            self.assertEqual("发布计划", items[0]["title"])
            self.assertEqual("pending", items[0]["status"])

            container = app.state.container
            self.assertEqual((), container.artifact_repository.list_artifacts())

    async def test_accept_via_api_creates_artifact(self) -> None:
        provider = TextProvider([[LONG_ANSWER], [EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(client)
            await self._wait_for_terminal(client, turn_id)
            items = await self._wait_for_proposals(client, conversation_id, 1)

            resolved = await client.post(
                f"/artifact-proposals/{items[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, resolved.status_code)
            body = resolved.json()
            self.assertEqual("accepted", body["proposal"]["status"])
            self.assertEqual("发布计划", body["artifact"]["title"])
            self.assertEqual(1, body["artifact"]["currentVersionOrdinal"])

            container = app.state.container
            artifacts = container.artifact_repository.list_artifacts()
            self.assertEqual(1, len(artifacts))
            version = container.artifact_repository.get_version(artifacts[0].id, 1)
            self.assertEqual(ArtifactVersionOperation.CREATE, version.operation)
            self.assertEqual(conversation_id, version.source_conversation_id)
            self.assertEqual(turn_id, version.source_turn_id)

    async def test_resolve_twice_conflicts(self) -> None:
        provider = TextProvider([[LONG_ANSWER], [EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(client)
            await self._wait_for_terminal(client, turn_id)
            items = await self._wait_for_proposals(client, conversation_id, 1)
            proposal_id = items[0]["id"]

            first = await client.post(
                f"/artifact-proposals/{proposal_id}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, first.status_code)
            second = await client.post(
                f"/artifact-proposals/{proposal_id}/resolve",
                json={"decision": "reject"},
            )
            self.assertEqual(409, second.status_code)

    async def test_provider_failure_keeps_turn_completed(self) -> None:
        provider = TextProvider([[LONG_ANSWER]], fail_from_index=1)
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(client)
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])
            await app.state.container.controller.drain_hooks()
            response = await client.get(
                f"/conversations/{conversation_id}/artifact-proposals"
            )
            self.assertEqual([], response.json()["items"])

    async def test_short_answer_skips_extraction(self) -> None:
        provider = TextProvider([["好的，已经记下了。"], [EXTRACTION_JSON]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id, turn_id = await self._run_turn(client)
            result = await self._wait_for_terminal(client, turn_id)
            self.assertEqual("completed", result["turnStatus"])
            await app.state.container.controller.drain_hooks()
            self.assertEqual(1, len(provider.requests))
            response = await client.get(
                f"/conversations/{conversation_id}/artifact-proposals"
            )
            self.assertEqual([], response.json()["items"])

    async def test_system_prompt_clause_follows_flag(self) -> None:
        provider = TextProvider([["好的。"]])
        async with local_client(self.database_path, provider) as (client, app):
            _, turn_id = await self._run_turn(client)
            await self._wait_for_terminal(client, turn_id)
            system_content = provider.requests[0].messages[0].content
            self.assertEqual("system", provider.requests[0].messages[0].role)
            self.assertIn("保留为文档", system_content)

        disabled_path = Path(self._temporary_directory.name) / "disabled.db"
        provider = TextProvider([["好的。"]])
        async with local_client(
            disabled_path, provider, artifact_proposals_enabled=False
        ) as (client, app):
            _, turn_id = await self._run_turn(client)
            await self._wait_for_terminal(client, turn_id)
            system_content = provider.requests[0].messages[0].content
            self.assertNotIn("保留为文档", system_content)

    async def test_pending_proposals_visible_in_context(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        proposals = SqliteArtifactProposalRepository(database)
        proposals.create_proposal(
            conversation_id="conv_seed",
            turn_id="turn_seed",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=LONG_ANSWER,
            reason="值得整体保留。",
        )

        provider = TextProvider([["好的。"]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            container = app.state.container
            container.artifact_proposal_repository.create_proposal(
                conversation_id=conversation_id,
                turn_id="turn_seed",
                title="本周总结",
                kind=ArtifactKind.MARKDOWN,
                content="长" * 500,
                reason="本周工作总结。",
            )
            created = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "request-1"},
                json={"content": "继续"},
            )
            await self._wait_for_terminal(client, created.json()["turnId"])
            system_content = provider.requests[0].messages[0].content
            self.assertIn("《本周总结》", system_content)
            self.assertNotIn("《发布计划》", system_content)


if __name__ == "__main__":
    unittest.main()
