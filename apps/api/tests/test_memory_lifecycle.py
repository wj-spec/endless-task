from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import MemoryKind, MemoryStatus
from endless_task.domain.repositories import InvalidStateError, ValidationError
from endless_task.memory import MemoryConflictService
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


class TextProvider:
    name = "lifecycle"

    def __init__(self, texts=(), fail_from_index=None) -> None:
        self.texts = list(texts)
        self.fail_from_index = fail_from_index
        self.requests = []

    async def stream(self, request, cancellation_token):
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_from_index is not None and index >= self.fail_from_index:
            raise ProviderError("provider_down", "冲突服务不可用", retryable=True)
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


class MemoryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "life.db")
        self.database.initialize()
        self.memories = SqliteMemoryRepository(self.database)
        self.proposals = SqliteMemoryProposalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_expire_state_machine_requires_reason(self) -> None:
        record = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在上海。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        with self.assertRaises(ValidationError):
            self.memories.expire_memory(record.id, reason="   ")

        expired = self.memories.expire_memory(
            record.id, reason="superseded", superseded_by="mem_other"
        )
        self.assertEqual(expired.status, MemoryStatus.EXPIRED)
        self.assertEqual(expired.expired_reason, "superseded")
        self.assertEqual(expired.superseded_by, "mem_other")
        self.assertIsNotNone(expired.expired_at)

        with self.assertRaises(InvalidStateError):
            self.memories.expire_memory(record.id, reason="superseded")

        self.assertEqual(self.memories.list_memories(), ())
        statuses = {
            item.id: item.status
            for item in self.memories.list_memories(include_deleted=True)
        }
        self.assertEqual(statuses[record.id], MemoryStatus.EXPIRED)

    def test_accept_duplicate_content_links_existing_memory(self) -> None:
        existing = self.memories.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        proposal = self.proposals.create_proposal(
            conversation_id="conv_2",
            turn_id="turn_2",
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            reason="重复内容。",
        )
        accepted, memory = self.proposals.accept_proposal(proposal.id)
        self.assertEqual(memory.id, existing.id)
        self.assertEqual(accepted.resolved_memory_id, existing.id)
        self.assertEqual(len(self.memories.list_memories()), 1)

    def test_conflict_service_supersedes_contradicted_memories(self) -> None:
        old = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在上海。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        new = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在杭州。",
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
        )
        provider = TextProvider([json.dumps({"superseded": [old.id]})])
        service = MemoryConflictService(
            provider=provider, memory_repository=self.memories, model="m"
        )
        expired = asyncio.run(service.resolve_conflicts_for(new))
        self.assertEqual([item.id for item in expired], [old.id])
        refreshed = self.memories.get_memory(old.id)
        self.assertEqual(refreshed.status, MemoryStatus.EXPIRED)
        self.assertEqual(refreshed.expired_reason, "superseded")
        self.assertEqual(refreshed.superseded_by, new.id)

    def test_conflict_failure_keeps_memories_active(self) -> None:
        old = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在上海。",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        new = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户住在杭州。",
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
        )
        provider = TextProvider(["不应到达"], fail_from_index=0)
        service = MemoryConflictService(
            provider=provider, memory_repository=self.memories, model="m"
        )
        self.assertEqual(asyncio.run(service.resolve_conflicts_for(new)), ())
        self.assertEqual(
            self.memories.get_memory(old.id).status, MemoryStatus.ACTIVE
        )


class MemoryLifecycleGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "gate.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(container, run_id: str):
        await wait_for_run_terminal(container, run_id)

    async def test_superseded_memory_is_excluded_from_injection(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        seeded = SqliteMemoryRepository(database).create_memory(
            kind=MemoryKind.FACT,
            content="用户住在上海。",
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )

        extraction = json.dumps(
            {
                "proposals": [
                    {"kind": "fact", "content": "用户住在杭州。", "reason": "变更。"}
                ]
            },
            ensure_ascii=False,
        )
        conflict = json.dumps({"superseded": [seeded.id]})
        provider = TextProvider(["好的。", extraction, conflict, "好的，按杭州来。"])
        async with local_client(self.database_path, provider) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "我搬到杭州了，记住。",
                idempotency_key="request-1",
            )
            await self._wait_for_terminal(container, handle["runId"])

            items = (
                await client.get(
                    f"/conversations/{conversation['id']}/memory-proposals"
                )
            ).json()["items"]
            self.assertEqual(len(items), 1)
            response = await client.post(
                f"/memory-proposals/{items[0]['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["memory"]["content"], "用户住在杭州。")
            self.assertEqual(payload["memory"]["sourceProposalId"], items[0]["id"])

            response = await client.get("/memories", params={"include_deleted": True})
            statuses = {item["id"]: item["status"] for item in response.json()["items"]}
            self.assertEqual(statuses[seeded.id], "expired")

            handle = await send_message(
                client,
                conversation["id"],
                "我住哪儿？",
                idempotency_key="request-2",
            )
            await self._wait_for_terminal(container, handle["runId"])

        system = "".join(
            (message.content or "") for message in provider.requests[3].messages
        )
        self.assertIn("[user_global] 用户住在杭州。", system)
        self.assertNotIn("用户住在上海", system)


if __name__ == "__main__":
    unittest.main()