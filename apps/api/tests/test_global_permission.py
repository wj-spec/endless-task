from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import PermissionMode
from endless_task.runtime import (
    FakeProvider,
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.storage import Database, SqlitePreferencesRepository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)
from tests.fixtures.workspace_client import create_bound_conversation


class LocalWriteTool:
    definition = ToolDefinition(
        name="write_note",
        description="把一条笔记保存到本机。",
        input_schema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.REQUIRED,
    )

    def __init__(self) -> None:
        self.calls = []

    def approval_prompt(self, call):
        del call
        return ToolApprovalPrompt(
            summary="允许保存这条本地笔记吗？",
            reason="内容将写入本机数据；只授权本次操作。",
            metadata={"effect": "local_write"},
        )

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content="saved")


class ExternalActionTool:
    definition = ToolDefinition(
        name="send_message",
        description="向外部服务发送一条消息。",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
    )

    def __init__(self) -> None:
        self.calls = []

    def approval_prompt(self, call):
        del call
        return ToolApprovalPrompt(
            summary="允许向外部服务发送消息吗？",
            reason="消息会离开本机；只授权本次操作。",
            metadata={"effect": "external_action"},
        )

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content="sent")


class DualToolProvider:
    """First request issues a local_write and an external_action call."""

    name = "dual-tool"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_local_1",
                name="write_note",
                arguments={"content": "本地笔记"},
            )
            yield ProviderToolCall(
                id="call_external_1",
                name="send_message",
                arguments={"text": "外部消息"},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("两个工具都已处理。")
        yield ProviderCompleted(finish_reason="stop")


class PermissionModeRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "preferences.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_default_mode_is_confirm_every_time(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        repository = SqlitePreferencesRepository(database)
        mode, updated_at = repository.get_permission_mode()
        self.assertEqual(PermissionMode.CONFIRM_EVERY_TIME, mode)
        self.assertTrue(updated_at)

    def test_permission_mode_persists(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        repository = SqlitePreferencesRepository(database)
        mode, _ = repository.set_permission_mode(PermissionMode.TRUST_ALL)
        self.assertEqual(PermissionMode.TRUST_ALL, mode)

        reopened = Database(self.database_path)
        reopened.initialize()
        reread = SqlitePreferencesRepository(reopened)
        mode, updated_at = reread.get_permission_mode()
        self.assertEqual(PermissionMode.TRUST_ALL, mode)
        self.assertTrue(updated_at)


class PermissionSettingsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self) -> httpx.AsyncClient:
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "api.db",
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
            ),
            provider=FakeProvider(chunks=("你好",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def test_default_mode_is_reported(self) -> None:
        async with await self._client() as client:
            response = await client.get("/settings/permissions")
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual("confirm_every_time", body["mode"])
            self.assertTrue(body["updatedAt"])

    async def test_escalation_requires_acknowledge(self) -> None:
        async with await self._client() as client:
            rejected = await client.post(
                "/settings/permissions",
                json={"mode": "trust_local_writes"},
            )
            self.assertEqual(400, rejected.status_code)
            self.assertEqual("invalid_request", rejected.json()["error"]["code"])

            confirmed = await client.post(
                "/settings/permissions",
                json={"mode": "trust_local_writes", "acknowledge": True},
            )
            self.assertEqual(200, confirmed.status_code)
            self.assertEqual("trust_local_writes", confirmed.json()["mode"])

    async def test_downgrade_does_not_require_acknowledge(self) -> None:
        async with await self._client() as client:
            await client.post(
                "/settings/permissions",
                json={"mode": "trust_all", "acknowledge": True},
            )
            downgraded = await client.post(
                "/settings/permissions",
                json={"mode": "confirm_every_time"},
            )
            self.assertEqual(200, downgraded.status_code)
            self.assertEqual("confirm_every_time", downgraded.json()["mode"])

    async def test_same_mode_does_not_require_acknowledge(self) -> None:
        async with await self._client() as client:
            response = await client.post(
                "/settings/permissions",
                json={"mode": "confirm_every_time"},
            )
            self.assertEqual(200, response.status_code)

    async def test_unknown_mode_is_rejected(self) -> None:
        async with await self._client() as client:
            response = await client.post(
                "/settings/permissions",
                json={"mode": "trust_universe", "acknowledge": True},
            )
            self.assertEqual(400, response.status_code)

    async def test_extra_fields_are_rejected(self) -> None:
        async with await self._client() as client:
            response = await client.post(
                "/settings/permissions",
                json={"mode": "trust_all", "acknowledge": True, "extra": 1},
            )
            self.assertEqual(400, response.status_code)


class PermissionCoverageRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple[httpx.AsyncClient, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(
        self,
        local_tool: LocalWriteTool,
        external_tool: ExternalActionTool,
    ) -> httpx.AsyncClient:
        registry = ToolRegistry()
        registry.register(local_tool)
        registry.register(external_tool)
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"api-{suffix}.db",
                runtime="v1",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
            ),
            provider=DualToolProvider(),
            tool_registry=registry,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._clients.append((client, lifespan))
        return client

    @staticmethod
    async def _wait_for_terminal(
        client: httpx.AsyncClient,
        turn_id: str,
    ) -> dict[str, object]:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            if response.json()["turnStatus"] in {"completed", "failed", "cancelled"}:
                return response.json()
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not reach a terminal state")

    @staticmethod
    async def _wait_for_approval(
        client: httpx.AsyncClient,
        turn_id: str,
    ) -> dict[str, object]:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            approval = response.json().get("pendingApproval")
            if approval:
                return approval
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not request approval")

    async def _start_turn(self, client: httpx.AsyncClient) -> str:
        conversation_id = (await create_bound_conversation(client))["id"]
        created = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "request-1"},
            json={"content": "请执行两个工具"},
        )
        return created.json()["turnId"]

    async def test_default_mode_still_waits_for_approval(self) -> None:
        local_tool = LocalWriteTool()
        external_tool = ExternalActionTool()
        client = await self._client(local_tool, external_tool)
        turn_id = await self._start_turn(client)
        approval = await self._wait_for_approval(client, turn_id)
        self.assertEqual("pending", approval["status"])
        self.assertEqual([], local_tool.calls)
        self.assertEqual([], external_tool.calls)

    async def test_trust_local_writes_auto_runs_local_but_not_external(self) -> None:
        local_tool = LocalWriteTool()
        external_tool = ExternalActionTool()
        client = await self._client(local_tool, external_tool)
        escalated = await client.post(
            "/settings/permissions",
            json={"mode": "trust_local_writes", "acknowledge": True},
        )
        self.assertEqual(200, escalated.status_code)

        turn_id = await self._start_turn(client)
        approval = await self._wait_for_approval(client, turn_id)
        self.assertIn("外部服务", approval["summary"])
        self.assertEqual(1, len(local_tool.calls))
        self.assertEqual([], external_tool.calls)

        resolved = await client.post(
            f"/approvals/{approval['id']}",
            json={"decision": "approve"},
        )
        self.assertEqual(200, resolved.status_code)
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("completed", result["turnStatus"])
        self.assertEqual("两个工具都已处理。", result["content"])
        self.assertEqual(1, len(external_tool.calls))

    async def test_trust_all_auto_runs_both_effects(self) -> None:
        local_tool = LocalWriteTool()
        external_tool = ExternalActionTool()
        client = await self._client(local_tool, external_tool)
        escalated = await client.post(
            "/settings/permissions",
            json={"mode": "trust_all", "acknowledge": True},
        )
        self.assertEqual(200, escalated.status_code)

        turn_id = await self._start_turn(client)
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("completed", result["turnStatus"])
        self.assertEqual("两个工具都已处理。", result["content"])
        self.assertEqual(1, len(local_tool.calls))
        self.assertEqual(1, len(external_tool.calls))


if __name__ == "__main__":
    unittest.main()