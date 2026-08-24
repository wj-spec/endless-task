from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.artifacts import parse_source_label
from endless_task.artifacts.proposal_service import ArtifactProposalService
from endless_task.domain.models import (
    ArtifactKind,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    ToolCallJournal,
)
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime import FakeProvider, ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
    SqliteMemoryRepository,
)

DOC_CONTENT = (
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


def extraction_json(labels=None, content=None) -> str:
    payload = {
        "proposal": {
            "title": "发布计划",
            "kind": "markdown",
            "content": DOC_CONTENT if content is None else content,
            "reason": "完整的发布计划文档，值得整体保留。",
        }
    }
    if labels is not None:
        payload["proposal"]["labels"] = labels
    return json.dumps(payload, ensure_ascii=False)


class TextProvider:
    name = "provenance"

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


class StubMemoryRepository:
    def __init__(self, records=()) -> None:
        self._records = {record.id: record for record in records}

    def get_memory(self, memory_id: str) -> MemoryRecord:
        record = self._records.get(memory_id)
        if record is None:
            raise NotFoundError(f"Memory not found: {memory_id}")
        return record

    def list_memories(self, *, include_deleted: bool = False):
        return tuple(
            record
            for record in self._records.values()
            if include_deleted or record.status is MemoryStatus.ACTIVE
        )


def memory_record(memory_id: str, *, status=MemoryStatus.ACTIVE) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        kind=MemoryKind.FACT,
        content=f"{memory_id} 的记忆内容。",
        status=status,
        source_conversation_id="conv_seed",
        source_turn_id="turn_seed",
        write_origin="confirmed_proposal",
        created_at="2026-08-23T00:00:00.000Z",
        updated_at="2026-08-23T00:00:00.000Z",
    )


def journal_call(call_id: str, file_id: str, *, status: str = "completed") -> ToolCallJournal:
    return ToolCallJournal(
        id=call_id,
        tool_name="read_text_file",
        arguments={"file_id": file_id},
        status=status,
    )


class SourceLabelGrammarTest(unittest.TestCase):
    def test_label_grammar_parsing(self) -> None:
        parsed = parse_source_label("file:file_123")
        self.assertEqual("file", parsed.kind)
        self.assertEqual("file_123", parsed.target_id)
        self.assertIsNone(parsed.line_start)

        ranged = parse_source_label("file:file_123:L3-L17")
        self.assertEqual("file", ranged.kind)
        self.assertEqual((3, 17), (ranged.line_start, ranged.line_end))

        memory = parse_source_label("memory:mem_0001")
        self.assertEqual("memory", memory.kind)
        self.assertEqual("mem_0001", memory.target_id)

        for invalid in (
            "file:",
            "file:bad id",
            "file:file_1:L0-L5",
            "file:file_1:L9-L2",
            "file:file_1:lines",
            "memory:",
            "memory:bad id",
            "read me.txt:L1-L7",
            "",
            None,
            42,
        ):
            self.assertEqual("text", parse_source_label(invalid).kind, repr(invalid))


class ProvenanceGenerationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteArtifactProposalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider, *, memory_repository=None, runtime_repository=None):
        return ArtifactProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            model="test-model",
            memory_repository=memory_repository,
            runtime_repository=runtime_repository,
        )

    async def test_file_labels_from_completed_reads(self) -> None:
        runtime = StubRuntimeRepository(
            (
                journal_call("call_1", "file_aaa"),
                journal_call("call_2", "file_bbb", status="failed"),
                journal_call("call_3", "file_aaa"),
                journal_call("call_4", "file_ccc", status="cancelled"),
                ToolCallJournal(
                    id="call_5",
                    tool_name="write_note",
                    arguments={"file_id": "file_ddd"},
                    status="completed",
                ),
            )
        )
        provider = TextProvider([extraction_json()])
        service = self._service(provider, runtime_repository=runtime)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="整理一下",
            assistant_message=DOC_CONTENT,
        )
        self.assertEqual(1, len(created))
        self.assertEqual(("file:file_aaa",), created[0].source_labels)

    async def test_memory_labels_are_validated(self) -> None:
        memories = StubMemoryRepository(
            (
                memory_record("mem_active"),
                memory_record("mem_deleted", status=MemoryStatus.DELETED),
            )
        )
        provider = TextProvider(
            [
                extraction_json(
                    labels=[
                        "memory:mem_active",
                        "memory:mem_deleted",
                        "memory:mem_missing",
                        "file:file_aaa",
                        "read me.txt:L1-L7",
                        "memory:mem_active",
                    ]
                )
            ]
        )
        service = self._service(provider, memory_repository=memories)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="整理一下",
            assistant_message=DOC_CONTENT,
        )
        self.assertEqual(("memory:mem_active",), created[0].source_labels)

        transcript = provider.requests[0].messages[1].content
        self.assertIn("当前可用的长期记忆清单", transcript)
        self.assertIn("memory:mem_active", transcript)
        self.assertNotIn("mem_deleted", transcript)

    async def test_label_order_and_cap(self) -> None:
        calls = tuple(
            journal_call(f"call_{index:02d}", f"file_{index:02d}")
            for index in range(25)
        )
        runtime = StubRuntimeRepository(calls)
        memories = StubMemoryRepository((memory_record("mem_active"),))
        provider = TextProvider([extraction_json(labels=["memory:mem_active"])])
        service = self._service(
            provider, memory_repository=memories, runtime_repository=runtime
        )
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="整理一下",
            assistant_message=DOC_CONTENT,
        )
        labels = created[0].source_labels
        self.assertEqual(20, len(labels))
        self.assertEqual("file:file_00", labels[0])
        self.assertTrue(all(item.startswith("file:") for item in labels))
        self.assertEqual(len(labels), len(set(labels)))

        mixed_runtime = StubRuntimeRepository(
            (journal_call("call_1", "file_only"),)
        )
        second_content = DOC_CONTENT + "\n\n## 附录\n第二轮整理的补充说明。"
        provider = TextProvider(
            [extraction_json(labels=["memory:mem_active"], content=second_content)]
        )
        service = self._service(
            provider, memory_repository=memories, runtime_repository=mixed_runtime
        )
        created = await service.generate_for_turn(
            conversation_id="conv_2",
            turn_id="turn_2",
            user_message="整理一下",
            assistant_message=DOC_CONTENT,
        )
        self.assertEqual(
            ("file:file_only", "memory:mem_active"), created[0].source_labels
        )


class ProvenanceFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteArtifactProposalRepository(self.database)
        self.artifacts = SqliteArtifactRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_labels_flow_into_version(self) -> None:
        labels = ("file:file_aaa", "memory:mem_active")
        proposal = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_1",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=DOC_CONTENT,
            reason="值得保留。",
            source_labels=labels,
        )
        self.assertEqual(labels, proposal.source_labels)
        self.assertEqual(
            labels, self.proposals.get_proposal(proposal.id).source_labels
        )

        _, artifact = self.proposals.accept_proposal(proposal.id)
        version = self.artifacts.get_version(artifact.id, 1)
        self.assertEqual(labels, version.source_labels)


class ProvenanceRenderingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_reference_resolution_and_fallback(self) -> None:
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

        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        uploaded = container.file_repository.create_file(
            conversation_id=conversation.id,
            original_name="me.txt",
            media_type="text/plain",
            content=b"hello",
        )
        memory = container.memory_repository.create_memory(
            kind=MemoryKind.FACT,
            content="用户在上海工作。",
            source_conversation_id=conversation.id,
            source_turn_id="turn_src",
        )
        snapshot = container.artifact_repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content=DOC_CONTENT,
            source_conversation_id=conversation.id,
            source_turn_id="turn_src",
            source_labels=(
                f"file:{uploaded.id}",
                f"memory:{memory.id}",
                "file:file_missing",
                "read me.txt:L1-L7",
            ),
        )
        artifact_id = snapshot.artifact.id

        detail = await client.get(f"/artifacts/{artifact_id}")
        self.assertEqual(200, detail.status_code)
        references = detail.json()["currentVersion"]["sourceReferences"]
        self.assertEqual(4, len(references))

        file_reference, memory_reference, missing_reference, text_reference = references
        self.assertEqual("file", file_reference["type"])
        self.assertTrue(file_reference["resolved"])
        self.assertEqual("me.txt", file_reference["fileName"])
        self.assertEqual(uploaded.id, file_reference["fileId"])
        self.assertEqual("memory", memory_reference["type"])
        self.assertTrue(memory_reference["resolved"])
        self.assertIn("上海", memory_reference["memorySnippet"])
        self.assertEqual("file", missing_reference["type"])
        self.assertFalse(missing_reference["resolved"])
        self.assertEqual("text", text_reference["type"])
        self.assertFalse(text_reference["resolved"])

        versions = await client.get(f"/artifacts/{artifact_id}/versions")
        items = versions.json()["items"]
        self.assertEqual(4, len(items[0]["sourceReferences"]))

        container.file_repository.delete_file(
            conversation_id=conversation.id, file_id=uploaded.id
        )
        container.memory_repository.delete_memory(memory.id)
        degraded = await client.get(f"/artifacts/{artifact_id}")
        degraded_references = degraded.json()["currentVersion"]["sourceReferences"]
        self.assertFalse(degraded_references[0]["resolved"])
        self.assertFalse(degraded_references[1]["resolved"])
        self.assertEqual(4, len(degraded_references))


if __name__ == "__main__":
    unittest.main()
