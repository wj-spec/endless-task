from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.artifacts.proposal_service import ArtifactProposalService
from endless_task.artifacts.read_tool import ReadArtifactTool
from endless_task.domain.models import (
    ArtifactKind,
    ArtifactVersionOperation,
    ToolCallJournal,
)
from endless_task.runtime import FakeProvider, ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
    ToolError,
)
from tests.fixtures.workspace_client import create_bound_conversation

FIRST_CONTENT = (
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
    "- 发布当天同步公告、更新日志与常见问题解答。\n"
    "- 内测期间设立固定反馈渠道，并由专人跟进关键问题。\n"
)

UPDATED_CONTENT = FIRST_CONTENT + (
    "\n## 验收标准\n- 全部发布门槛测试通过，且无未关闭的阻断问题。\n"
    "- 新手引导完成率在内测样本中不低于八成。\n"
    "- 发布后三天内完成首轮反馈归类，并给出修复排期。\n"
)


def update_extraction_json(target, *, content=None) -> str:
    payload = {
        "proposal": {
            "title": "发布计划",
            "kind": "markdown",
            "content": UPDATED_CONTENT if content is None else content,
            "reason": "按用户要求补充了验收标准。",
            "labels": [],
            "target": target,
        }
    }
    return json.dumps(payload, ensure_ascii=False)


class TextProvider:
    name = "chat-continue"

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


class StubRuntimeRepository:
    def __init__(self, calls=()) -> None:
        self._calls = tuple(calls)

    def list_tool_calls(self, turn_id: str):
        return self._calls


def make_call(arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="read_artifact",
        arguments=arguments,
        status=ToolCallStatus.RUNNING,
        created_at="2026-08-24T00:00:00.000Z",
    )


class ReadArtifactToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.repository = SqliteArtifactRepository(self.database)
        self.tool = ReadArtifactTool(self.repository)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_read_artifact_tool(self) -> None:
        self.assertEqual(ToolEffect.READ_ONLY, self.tool.definition.effect)
        self.assertEqual(ToolApprovalMode.AUTO, self.tool.definition.approval_mode)

        snapshot = self.repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=FIRST_CONTENT,
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        from endless_task.runtime.cancellation import CancellationToken

        result = await self.tool.execute(
            make_call({"artifact_id": snapshot.artifact.id}), CancellationToken()
        )
        self.assertIn("[来源：《发布计划》v1]", result.content)
        self.assertIn(FIRST_CONTENT, result.content)
        self.assertEqual(1, result.structured_content["ordinal"])

        with self.assertRaises(ToolError):
            await self.tool.execute(
                make_call({"artifact_id": "art_missing"}), CancellationToken()
            )

        self.repository.delete_artifact(snapshot.artifact.id)
        with self.assertRaises(ToolError):
            await self.tool.execute(
                make_call({"artifact_id": snapshot.artifact.id}), CancellationToken()
            )


class ChatContinueServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteArtifactProposalRepository(self.database)
        self.artifacts = SqliteArtifactRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_artifact(self, *, labels=()) -> str:
        snapshot = self.artifacts.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=FIRST_CONTENT,
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
            source_labels=labels,
        )
        return snapshot.artifact.id

    def _service(self, provider, *, runtime_repository=None):
        return ArtifactProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            model="test-model",
            runtime_repository=runtime_repository,
            artifact_repository=self.artifacts,
        )

    async def test_invalid_target_and_identical_content_are_skipped(self) -> None:
        provider = TextProvider([update_extraction_json("art_missing")])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="补充验收标准",
            assistant_message=UPDATED_CONTENT,
        )
        self.assertEqual((), created)

        artifact_id = self._seed_artifact()
        provider = TextProvider(
            [update_extraction_json(artifact_id, content=FIRST_CONTENT)]
        )
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="原样保存",
            assistant_message=FIRST_CONTENT,
        )
        self.assertEqual((), created)

        deleted_id = self._seed_artifact()
        self.artifacts.delete_artifact(deleted_id)
        provider = TextProvider([update_extraction_json(deleted_id)])
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="补充验收标准",
            assistant_message=UPDATED_CONTENT,
        )
        self.assertEqual((), created)

    async def test_labels_are_inherited_on_chat_continue(self) -> None:
        artifact_id = self._seed_artifact(labels=("file:file_aaa",))
        runtime = StubRuntimeRepository(
            (
                ToolCallJournal(
                    id="call_1",
                    tool_name="read_text_file",
                    arguments={"file_id": "file_bbb"},
                    status="completed",
                ),
            )
        )
        provider = TextProvider([update_extraction_json(artifact_id)])
        service = self._service(provider, runtime_repository=runtime)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="补充验收标准",
            assistant_message=UPDATED_CONTENT,
        )
        self.assertEqual(1, len(created))
        self.assertEqual(
            ("file:file_aaa", "file:file_bbb"), created[0].source_labels
        )
        self.assertEqual(artifact_id, created[0].target_artifact_id)

        _, artifact = self.proposals.accept_proposal(created[0].id)
        version = self.artifacts.get_version(artifact.id, 2)
        self.assertEqual(("file:file_aaa", "file:file_bbb"), version.source_labels)
        self.assertEqual(ArtifactVersionOperation.CHAT_CONTINUE, version.operation)


class ChatContinueGateTest(unittest.IsolatedAsyncioTestCase):
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

    async def _client(self, provider):
        app = create_app(
            settings=AppSettings(
                database_path=self.database_path,
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=True,
            ),
            provider=provider,
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

    async def test_artifact_list_in_context(self) -> None:
        provider = TextProvider([["好的。"]])
        client, app = await self._client(provider)
        container = app.state.container
        snapshot = container.artifact_repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=FIRST_CONTENT,
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )
        conversation_id = (await create_bound_conversation(client))["id"]
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "request-1"},
            json={"content": "帮我看看"},
        )
        await self._wait_for_terminal(client, created.json()["turnId"])
        system_content = provider.requests[0].messages[0].content
        self.assertIn(f"artifact:{snapshot.artifact.id}", system_content)
        self.assertIn("《发布计划》", system_content)
        self.assertNotIn("功能冻结", system_content)

    async def test_update_proposal_appends_chat_continue_version(self) -> None:
        provider = TextProvider([["占位回复"]])
        client, app = await self._client(provider)
        container = app.state.container
        snapshot = container.artifact_repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=FIRST_CONTENT,
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )
        artifact_id = snapshot.artifact.id

        provider = TextProvider([[UPDATED_CONTENT], [update_extraction_json(artifact_id)]])
        client, app = await self._client(provider)

        conversation_id = (await create_bound_conversation(client))["id"]
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "request-1"},
            json={"content": "补充验收标准"},
        )
        turn_id = created.json()["turnId"]
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("completed", result["turnStatus"])

        items = await self._wait_for_proposals(client, conversation_id, 1)
        self.assertEqual(artifact_id, items[0]["targetArtifactId"])
        self.assertEqual(1, items[0]["baseVersionOrdinal"])
        self.assertEqual("pending", items[0]["status"])
        self.assertEqual(
            1,
            container.artifact_repository.get_artifact(artifact_id)
            .current_version_ordinal,
        )

        resolved = await client.post(
            f"/artifact-proposals/{items[0]['id']}/resolve",
            json={"decision": "accept"},
        )
        self.assertEqual(200, resolved.status_code)
        body = resolved.json()
        self.assertEqual(artifact_id, body["artifact"]["id"])
        self.assertEqual(2, body["artifact"]["currentVersionOrdinal"])

        version = container.artifact_repository.get_version(artifact_id, 2)
        self.assertEqual(ArtifactVersionOperation.CHAT_CONTINUE, version.operation)
        self.assertEqual(UPDATED_CONTENT.strip(), version.content)
        self.assertEqual(conversation_id, version.source_conversation_id)
        self.assertEqual(turn_id, version.source_turn_id)

    async def test_accept_after_target_deleted_conflicts(self) -> None:
        provider = TextProvider([["占位回复"]])
        client, app = await self._client(provider)
        container = app.state.container
        snapshot = container.artifact_repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=FIRST_CONTENT,
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )
        proposal = container.artifact_proposal_repository.create_proposal(
            conversation_id="conv_x",
            turn_id="turn_x",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=UPDATED_CONTENT,
            reason="补充验收标准。",
            target_artifact_id=snapshot.artifact.id,
        )
        container.artifact_repository.delete_artifact(snapshot.artifact.id)
        resolved = await client.post(
            f"/artifact-proposals/{proposal.id}/resolve",
            json={"decision": "accept"},
        )
        self.assertEqual(409, resolved.status_code)


if __name__ == "__main__":
    unittest.main()