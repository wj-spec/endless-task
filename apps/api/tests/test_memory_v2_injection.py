from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind
from endless_task.memory import MemoryProposalService
from endless_task.runtime import P0ContextBuilder, ProviderCompleted, ProviderTextDelta
from endless_task.runtime.provider import (
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
)
from endless_task.runtime_v2 import Actor, MemoryScope, TranscriptEntryType
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
    SqliteRuntimeV2MemoryRepository,
    SqliteRuntimeV2Repository,
)


class TextProvider:
    name = "v2-injection"

    def __init__(self, texts: Sequence[str] = ()) -> None:
        self.texts = list(texts)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token,
    ) -> AsyncIterator[ProviderStreamEvent]:
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        if self.texts:
            for chunk in self.texts[min(index, len(self.texts) - 1)]:
                yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, provider, *, runtime: str = "v2"):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            runtime=runtime,
            knowledge_proposals_enabled=False,
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


class DoubleInjectionBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "double.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.memory_repository = SqliteMemoryRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.v2_memory_repository = SqliteRuntimeV2MemoryRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _conversation_with_both_memories(self):
        conversation = self.chat_repository.create_conversation()
        self.memory_repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="V1 记忆：用户喜欢简洁。",
            source_conversation_id=conversation.id,
            source_turn_id="turn_1",
        )
        self.v2_memory_repository.create_memory(
            scope=MemoryScope.USER_GLOBAL,
            kind="fact",
            content="V2 记忆：用户在北京工作。",
            conversation_id=conversation.id,
        )
        return conversation


class SystemMessagesMemoryLayerTest(DoubleInjectionBase):
    def _builder(self) -> P0ContextBuilder:
        return P0ContextBuilder(
            self.chat_repository,
            system_prompt="SYSTEM",
            memory_repository=self.memory_repository,
        )

    def test_default_includes_v1_memory(self) -> None:
        conversation = self._conversation_with_both_memories()
        messages = self._builder().build_system_messages(
            conversation.id, "当前问题", turn_id="turn_9", workspace_id=None
        )
        contents = "\n".join(message.content for message in messages)
        self.assertIn("V1 记忆", contents)

    def test_v2_mode_excludes_v1_memory(self) -> None:
        conversation = self._conversation_with_both_memories()
        messages = self._builder().build_system_messages(
            conversation.id,
            "当前问题",
            turn_id="turn_9",
            workspace_id=None,
            include_v1_memory=False,
        )
        contents = "\n".join(message.content for message in messages)
        self.assertNotIn("V1 记忆", contents)


class AutoWriteTargetTest(DoubleInjectionBase, unittest.IsolatedAsyncioTestCase):
    async def test_auto_write_target_routes_to_v2(self) -> None:
        proposals = SqliteMemoryProposalRepository(self.database)
        payload = json.dumps(
            {
                "proposals": [
                    {
                        "kind": "fact",
                        "content": "用户养了一只猫。",
                        "reason": "明确陈述",
                        "confidence": "high",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider([payload])
        service = MemoryProposalService(
            provider=provider,
            proposal_repository=proposals,
            memory_repository=self.memory_repository,
            model="test-model",
            auto_fact_enabled=True,
        )
        conversation = self.chat_repository.create_conversation()

        written: list[tuple[str, str, str]] = []

        def write_target(kind, content, conversation_id, turn_id):
            del kind, turn_id
            written.append((content, conversation_id, ""))

        await service.generate_for_turn(
            conversation_id=conversation.id,
            turn_id="turn_1",
            user_message="记住：我养了一只猫。",
            assistant_message="好的。",
            auto_write_target=write_target,
        )
        # v2 会话:auto_fact 路由到注入的写入目标,不写 v1 表。
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][0], "用户养了一只猫。")
        self.assertEqual(self.memory_repository.list_memories(), ())


class V2EndToEndInjectionTest(DoubleInjectionBase, unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _wait_for_run(client, conversation_id: str, run_id: str):
        for _ in range(300):
            snapshot = (
                await client.get(
                    f"/api/v2/conversations/{conversation_id}/snapshot"
                )
            ).json()
            run_state = snapshot.get("runState") or {}
            if run_state.get("runId") == run_id and run_state.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return snapshot
            await asyncio.sleep(0.01)
        raise AssertionError("v2 run did not reach a terminal state")

    async def test_v2_request_injects_v2_memory_only(self) -> None:
        conversation = self._conversation_with_both_memories()
        provider = TextProvider(["好的。"])
        async with local_client(self.database_path, provider) as client:
            response = await client.post(
                f"/api/v2/conversations/{conversation.id}/messages",
                headers={"Idempotency-Key": "request-1"},
                json={"content": "你好"},
            )
            self.assertEqual(response.status_code, 202)
            body = response.json()
            await self._wait_for_run(client, conversation.id, body["runId"])

        self.assertTrue(provider.requests)
        contents = [m.content for m in provider.requests[0].messages]
        joined = "\n".join(contents)
        # v2 六层作用域记忆注入。
        self.assertIn("V2 记忆", joined)
        self.assertIn("<runtime-memory>", joined)
        # v1 记忆不再进入 v2 请求(单来源,无双注入)。
        self.assertNotIn("V1 记忆", joined)


class V2ProposalResolutionTest(DoubleInjectionBase, unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _wait_for_run(client, conversation_id: str, run_id: str):
        for _ in range(300):
            snapshot = (
                await client.get(
                    f"/api/v2/conversations/{conversation_id}/snapshot"
                )
            ).json()
            run_state = snapshot.get("runState") or {}
            if run_state.get("runId") == run_id and run_state.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return snapshot
            await asyncio.sleep(0.01)
        raise AssertionError("v2 run did not reach a terminal state")

    async def test_confirmed_proposal_writes_v2_user_global(self) -> None:
        conversation = self.chat_repository.create_conversation()
        extraction = json.dumps(
            {
                "proposals": [
                    {
                        "kind": "preference",
                        "content": "用户偏好中文回答。",
                        "reason": "明确表达",
                        "confidence": "high",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的。", extraction, "已记住。"])
        async with local_client(self.database_path, provider) as client:
            response = await client.post(
                f"/api/v2/conversations/{conversation.id}/messages",
                headers={"Idempotency-Key": "request-1"},
                json={"content": "请记住我喜欢中文回答。"},
            )
            self.assertEqual(response.status_code, 202)
            await self._wait_for_run(client, conversation.id, response.json()["runId"])

            proposals = []
            for _ in range(300):
                proposals = (
                    await client.get(
                        f"/conversations/{conversation.id}/memory-proposals"
                    )
                ).json()["items"]
                if len(proposals) == 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(proposals), 1)
            response = await client.post(
                f"/memory-proposals/{proposals[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 200)

        # v2 会话确认的记忆进入 v2 user_global(读取端单来源)。
        memory = self.v2_memory_repository.find_active_user_global_memory(
            conversation_id=conversation.id,
            content="用户偏好中文回答。",
        )
        self.assertIsNotNone(memory)
        self.assertEqual(memory.scope, MemoryScope.USER_GLOBAL)


class V1MemoryMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "migrate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_v1_memories_migrate_to_v2_user_global(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        chat = SqliteChatRepository(database)
        memory = SqliteMemoryRepository(database)
        conversation = chat.create_conversation()
        memory.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="迁移记忆：用户喜欢简洁。",
            source_conversation_id=conversation.id,
            source_turn_id="turn_1",
        )
        # 模拟 049 之前的存量:手动执行迁移 SQL(迁移系统本身只执行一次)。
        from importlib import resources

        migration_root = resources.files("endless_task.storage.migrations")
        sql = (
            migration_root.joinpath("049_runtime_v2_migrate_v1_memories.sql")
            .read_text(encoding="utf-8")
        )
        with database.connect() as connection:
            connection.executescript(sql)

        v2 = SqliteRuntimeV2MemoryRepository(database)
        migrated = v2.find_active_user_global_memory(
            conversation_id=conversation.id,
            content="迁移记忆：用户喜欢简洁。",
        )
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.scope, MemoryScope.USER_GLOBAL)

    def test_migration_skips_orphaned_sources(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        memory = SqliteMemoryRepository(database)
        memory.create_memory(
            kind=MemoryKind.FACT,
            content="孤儿来源记忆。",
            source_conversation_id="conv_ghost",
            source_turn_id="turn_1",
        )
        from importlib import resources

        migration_root = resources.files("endless_task.storage.migrations")
        sql = (
            migration_root.joinpath("049_runtime_v2_migrate_v1_memories.sql")
            .read_text(encoding="utf-8")
        )
        with database.connect() as connection:
            connection.executescript(sql)

        with database.connect() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM v2_runtime_memories
                WHERE conversation_id = 'conv_ghost'
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
