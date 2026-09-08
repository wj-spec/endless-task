from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import FinishReason, MemoryKind
from endless_task.runtime import P0ContextBuilder, ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)

from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation
from tests.test_sqlite_chat_repository import SequenceClock, SequenceIdFactory


class TextProvider:
    name = "injection"

    def __init__(self, texts=()) -> None:
        self.texts = list(texts)
        self.requests = []

    async def stream(self, request, cancellation_token):
        index = len(self.requests)
        self.requests.append(request)
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


class MemoryInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "context.db")
        self.database.initialize()
        self.clock = SequenceClock()
        self.ids = SequenceIdFactory()
        self.chat_repository = SqliteChatRepository(
            self.database, clock=self.clock, id_factory=self.ids
        )
        self.memory_repository = SqliteMemoryRepository(
            self.database, clock=self.clock, id_factory=self.ids
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _builder(self, **overrides) -> P0ContextBuilder:
        return P0ContextBuilder(
            self.chat_repository,
            system_prompt="基础提示词",
            memory_repository=self.memory_repository,
            **overrides,
        )

    def _new_turn(self):
        conversation = self.chat_repository.create_conversation()
        return self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="你好",
        )

    def test_active_memories_are_injected_into_system_content(self) -> None:
        kept = self.memory_repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        removed = self.memory_repository.create_memory(
            kind=MemoryKind.FACT,
            content="用户已删除的记忆。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.memory_repository.delete_memory(removed.id)

        snapshot = self._new_turn()
        built = self._builder().build(snapshot.turn.id)
        system = built.messages[0]
        self.assertEqual(system.role, "system")
        self.assertIn("用户确认后写入的长期记忆", system.content)
        self.assertIn("- (preference) 用户偏好简洁回答。", system.content)
        self.assertNotIn("用户已删除的记忆", system.content)

    def test_injection_respects_count_and_char_limits(self) -> None:
        for index in range(25):
            self.memory_repository.create_memory(
                kind=MemoryKind.FACT,
                content=f"记忆条目{index:02d}。",
                source_conversation_id="conv_1",
                source_turn_id="turn_1",
            )
        snapshot = self._new_turn()
        built = self._builder().build(snapshot.turn.id)
        lines = [
            line
            for line in built.messages[0].content.splitlines()
            if line.startswith("- (")
        ]
        self.assertEqual(len(lines), 20)

    def test_injection_respects_char_limit(self) -> None:
        # B3：字符预算只放得下一条时，优先注入"更该留"的那条（钉住 > 重要 > 常用 > 新近）。
        long_memory = self.memory_repository.create_memory(
            kind=MemoryKind.FACT,
            content="长" * 1000,
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.memory_repository.set_memory_pinned(long_memory.id, True)
        self.memory_repository.create_memory(
            kind=MemoryKind.FACT,
            content="短条目。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        snapshot = self._new_turn()
        built = self._builder(max_memory_chars=1015).build(snapshot.turn.id)
        content = built.messages[0].content
        self.assertIn("- (fact) " + "长" * 1000, content)
        self.assertNotIn("短条目", content)

    def test_injection_drops_the_rest_when_char_limit_is_tight(self) -> None:
        for index in range(3):
            self.memory_repository.create_memory(
                kind=MemoryKind.FACT,
                content=f"条目{index}。" + "补" * 400,
                source_conversation_id="conv_1",
                source_turn_id="turn_1",
            )
        snapshot = self._new_turn()
        built = self._builder(max_memory_chars=600).build(snapshot.turn.id)
        lines = [
            line
            for line in built.messages[0].content.splitlines()
            if line.startswith("- (")
        ]
        self.assertEqual(1, len(lines))


class MemoryInjectionGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(container, run_id: str):
        await wait_for_run_terminal(container, run_id)

    async def test_confirmed_memory_reaches_next_turn_system_message(self) -> None:
        extraction = json.dumps(
            {
                "proposals": [
                    {
                        "kind": "preference",
                        "content": "用户偏好本地优先的方案。",
                        "reason": "明确表达。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = TextProvider(["好的。", extraction, "我会记住你的偏好。"])
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "请记住我喜欢本地优先。",
                idempotency_key="request-1",
            )
            await self._wait_for_terminal(container, handle["runId"])

            proposals = []
            for _ in range(200):
                proposals = (
                    await client.get(
                        f"/conversations/{conversation['id']}/memory-proposals"
                    )
                ).json()["items"]
                if len(proposals) == 1:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(len(proposals), 1)
            response = await client.post(
                f"/memory-proposals/{proposals[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 200)

            handle = await send_message(
                client,
                conversation["id"],
                "帮我选一个技术方向。",
                idempotency_key="request-2",
            )
            await self._wait_for_terminal(container, handle["runId"])

        second_turn_request = provider.requests[2]
        system_contents = "".join(
            (message.content or "") for message in second_turn_request.messages
        )
        self.assertIn("[user_global] 用户偏好本地优先的方案。", system_contents)
        first_turn_request = provider.requests[0]
        first_contents = "".join(
            (message.content or "") for message in first_turn_request.messages
        )
        self.assertNotIn("用户偏好本地优先的方案。", first_contents)


if __name__ == "__main__":
    unittest.main()