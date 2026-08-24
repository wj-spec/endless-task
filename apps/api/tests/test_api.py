from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    FakeProvider,
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


class FileReadingProvider:
    name = "file-reading"

    def __init__(self, file_id: str) -> None:
        self.file_id = file_id
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_1",
                name="read_text_file",
                arguments={"file_id": self.file_id},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        source = request.messages[-1].content.splitlines()[0]
        yield ProviderTextDelta(f"文件内容已读取。{source}")
        yield ProviderCompleted(finish_reason="stop")


class DelayedProvider:
    name = "delayed"

    async def stream(self, request, cancellation_token):
        del request
        await asyncio.sleep(0.04)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("稍后回答")
        yield ProviderCompleted(finish_reason="stop")


class ApprovalProvider:
    name = "approval-provider"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_write_1",
                name="write_note",
                arguments={"content": "一条本地笔记"},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        tool_result = request.messages[-1].content
        text = "笔记已保存。" if tool_result == "saved" else "好的，我没有保存笔记。"
        yield ProviderTextDelta(text)
        yield ProviderCompleted(finish_reason="stop")


class ApprovalWriteTool:
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


class LocalApiTest(unittest.IsolatedAsyncioTestCase):
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
        provider=None,
        *,
        tool_registry=None,
        heartbeat_seconds: float = 0.01,
        max_message_characters: int = 100_000,
        max_file_bytes: int = 1_000_000,
    ) -> httpx.AsyncClient:
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"api-{suffix}.db",
                memory_proposals_enabled=False,
                heartbeat_seconds=heartbeat_seconds,
                max_message_characters=max_message_characters,
                max_file_bytes=max_file_bytes,
            ),
            provider=provider or FakeProvider(chunks=("你好", "，本地 API。")),
            tool_registry=tool_registry,
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
    async def _create_turn(
        client: httpx.AsyncClient,
        conversation_id: str,
        key: str = "request-1",
    ) -> httpx.Response:
        return await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": key},
            json={"content": "你好"},
        )

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

    async def test_required_tool_waits_for_one_time_approval_then_resumes(self) -> None:
        provider = ApprovalProvider()
        tool = ApprovalWriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        client = await self._client(provider, tool_registry=registry)
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)
        turn_id = created.json()["turnId"]

        approval = await self._wait_for_approval(client, turn_id)
        self.assertEqual("pending", approval["status"])
        self.assertEqual("允许保存这条本地笔记吗？", approval["summary"])
        self.assertNotIn("content", approval["metadata"])
        self.assertEqual([], tool.calls)

        resolved = await client.post(
            f"/approvals/{approval['id']}",
            json={"decision": "approve"},
        )
        self.assertEqual(200, resolved.status_code)
        self.assertEqual("approved", resolved.json()["status"])
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("completed", result["turnStatus"])
        self.assertEqual("笔记已保存。", result["content"])
        self.assertEqual(1, len(tool.calls))
        self.assertEqual("completed", result["activities"][0]["status"])

        conflicting = await client.post(
            f"/approvals/{approval['id']}",
            json={"decision": "deny"},
        )
        self.assertEqual(409, conflicting.status_code)

    async def test_denied_tool_is_not_executed_and_model_can_continue(self) -> None:
        provider = ApprovalProvider()
        tool = ApprovalWriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        client = await self._client(provider, tool_registry=registry)
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)
        turn_id = created.json()["turnId"]
        approval = await self._wait_for_approval(client, turn_id)

        resolved = await client.post(
            f"/approvals/{approval['id']}",
            json={"decision": "deny"},
        )
        self.assertEqual("denied", resolved.json()["status"])
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("好的，我没有保存笔记。", result["content"])
        self.assertEqual([], tool.calls)
        self.assertEqual([], result["activities"])

    async def test_turn_cancel_also_cancels_a_pending_approval(self) -> None:
        provider = ApprovalProvider()
        tool = ApprovalWriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        client = await self._client(provider, tool_registry=registry)
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)
        turn_id = created.json()["turnId"]
        approval = await self._wait_for_approval(client, turn_id)

        cancelled = await client.post(f"/turns/{turn_id}/cancel")
        self.assertEqual(200, cancelled.status_code)
        result = await self._wait_for_terminal(client, turn_id)
        self.assertEqual("cancelled", result["turnStatus"])
        self.assertEqual([], tool.calls)

        stale = await client.post(
            f"/approvals/{approval['id']}",
            json={"decision": "approve"},
        )
        self.assertEqual(409, stale.status_code)

    async def test_empty_conversation_is_reused_until_first_turn(self) -> None:
        client = await self._client()
        first = await client.post("/conversations")
        second = await client.post("/conversations")

        self.assertEqual(201, first.status_code)
        self.assertEqual(first.json()["id"], second.json()["id"])

        turn = await self._create_turn(client, first.json()["id"])
        self.assertEqual(202, turn.status_code)
        await self._wait_for_terminal(client, turn.json()["turnId"])
        third = await client.post("/conversations")
        self.assertNotEqual(first.json()["id"], third.json()["id"])

    async def test_conversation_text_file_upload_listing_and_delete(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]

        uploaded = await client.post(
            f"/conversations/{conversation_id}/files",
            params={"filename": "notes.md"},
            headers={"Content-Type": "text/markdown"},
            content="第一行\n第二行".encode(),
        )

        self.assertEqual(201, uploaded.status_code)
        self.assertEqual("notes.md", uploaded.json()["originalName"])
        snapshot = await client.get(f"/conversations/{conversation_id}")
        self.assertEqual(
            [uploaded.json()["id"]],
            [item["id"] for item in snapshot.json()["files"]],
        )

        deleted = await client.delete(
            f"/conversations/{conversation_id}/files/{uploaded.json()['id']}"
        )
        self.assertEqual(204, deleted.status_code)
        snapshot = await client.get(f"/conversations/{conversation_id}")
        self.assertEqual([], snapshot.json()["files"])

    async def test_file_upload_enforces_streaming_size_limit(self) -> None:
        client = await self._client(max_file_bytes=4)
        conversation_id = (await client.post("/conversations")).json()["id"]

        response = await client.post(
            f"/conversations/{conversation_id}/files",
            params={"filename": "notes.txt"},
            content=b"12345",
        )

        self.assertEqual(413, response.status_code)
        self.assertEqual("file_too_large", response.json()["error"]["code"])

    async def test_uploaded_file_can_be_read_through_the_chat_agent_loop(self) -> None:
        initial_client = await self._client()
        conversation_id = (await initial_client.post("/conversations")).json()["id"]
        uploaded = await initial_client.post(
            f"/conversations/{conversation_id}/files",
            params={"filename": "brief.md"},
            content="项目目标".encode(),
        )
        file_id = uploaded.json()["id"]

        await initial_client.aclose()
        _, initial_lifespan = self._clients.pop()
        await initial_lifespan.__aexit__(None, None, None)
        provider = FileReadingProvider(file_id)
        client = await self._client(provider)
        restored_conversation = (await client.get(f"/conversations/{conversation_id}"))
        self.assertEqual(200, restored_conversation.status_code)

        created = await self._create_turn(client, conversation_id)
        result = await self._wait_for_terminal(client, created.json()["turnId"])

        self.assertEqual("completed", result["turnStatus"])
        self.assertIn("[来源：brief.md:L1-L1]", result["content"])
        self.assertEqual(
            [
                {
                    "status": "completed",
                    "message": "已读取 brief.md",
                }
            ],
            [
                {"status": item["status"], "message": item["message"]}
                for item in result["activities"]
            ],
        )
        self.assertIn(
            "read_text_file",
            [tool.name for tool in provider.requests[0].tools],
        )
        self.assertIn(file_id, provider.requests[0].messages[0].content)
        restored = await client.get(f"/conversations/{conversation_id}")
        self.assertEqual(
            "已读取 brief.md",
            restored.json()["turns"][-1]["activities"][0]["message"],
        )

    async def test_failed_file_tool_exposes_only_a_natural_activity(self) -> None:
        client = await self._client(FileReadingProvider("file_missing"))
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)

        result = await self._wait_for_terminal(client, created.json()["turnId"])

        # 工具失败反馈给模型后由 Assistant 解释，而不是让整个 Turn 失败。
        self.assertEqual("completed", result["turnStatus"])
        self.assertIn("工具执行失败", result["content"])
        self.assertEqual(1, len(result["activities"]))
        activity = result["activities"][0]
        self.assertEqual("failed", activity["status"])
        self.assertEqual("读取 已上传文档 失败，可以重试", activity["message"])
        self.assertNotIn("arguments", activity)

    async def test_turn_command_snapshot_and_sse_replay(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)
        self.assertEqual(202, created.status_code)
        payload = created.json()

        snapshot = await self._wait_for_terminal(client, payload["turnId"])
        self.assertEqual("completed", snapshot["turnStatus"])
        self.assertEqual("你好，本地 API。", snapshot["content"])

        replay = await client.get(payload["eventsUrl"])
        self.assertEqual(200, replay.status_code)
        self.assertTrue(replay.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: turn.started", replay.text)
        self.assertIn("event: turn.completed", replay.text)

        after_two = await client.get(
            payload["eventsUrl"],
            headers={"Last-Event-ID": f"{payload['turnId']}:2"},
        )
        self.assertNotIn(f"id: {payload['turnId']}:1\n", after_two.text)
        self.assertNotIn(f"id: {payload['turnId']}:2\n", after_two.text)
        self.assertIn(f"id: {payload['turnId']}:3\n", after_two.text)

        envelopes = [
            json.loads(line.removeprefix("data: "))
            for line in replay.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            list(range(1, len(envelopes) + 1)),
            [event["sequence"] for event in envelopes],
        )

    async def test_sse_emits_heartbeat_while_waiting(self) -> None:
        client = await self._client(DelayedProvider(), heartbeat_seconds=0.005)
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)

        stream = await client.get(created.json()["eventsUrl"])

        self.assertIn(": heartbeat\n\n", stream.text)
        self.assertIn("event: turn.completed", stream.text)

    async def test_idempotent_create_and_regenerate_return_original_ids(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        first = await self._create_turn(client, conversation_id)
        first_payload = first.json()
        await self._wait_for_terminal(client, first_payload["turnId"])

        duplicate = await self._create_turn(client, conversation_id)
        self.assertEqual(first_payload, duplicate.json())

        regenerated = await client.post(
            f"/turns/{first_payload['turnId']}/regenerate",
            headers={"Idempotency-Key": "regenerate-1"},
        )
        self.assertEqual(202, regenerated.status_code)
        regenerated_payload = regenerated.json()
        await self._wait_for_terminal(client, first_payload["turnId"])

        duplicate_regenerate = await client.post(
            f"/turns/{first_payload['turnId']}/regenerate",
            headers={"Idempotency-Key": "regenerate-1"},
        )
        self.assertEqual(regenerated_payload, duplicate_regenerate.json())
        self.assertNotEqual(
            first_payload["responseVariantId"],
            regenerated_payload["responseVariantId"],
        )

        selected = await client.post(
            f"/turns/{first_payload['turnId']}/response-variants/"
            f"{first_payload['responseVariantId']}/select"
        )
        self.assertEqual(200, selected.status_code)
        self.assertEqual(
            first_payload["responseVariantId"],
            selected.json()["responseVariantId"],
        )

    async def test_errors_use_stable_envelope(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]

        missing_key = await client.post(
            f"/conversations/{conversation_id}/turns",
            json={"content": "你好"},
        )
        self.assertEqual(400, missing_key.status_code)
        self.assertEqual("invalid_request", missing_key.json()["error"]["code"])
        self.assertIn("correlationId", missing_key.json()["error"])

        missing = await client.get("/turns/turn_missing")
        self.assertEqual(404, missing.status_code)
        self.assertEqual("not_found", missing.json()["error"]["code"])

        invalid_cursor = await client.get(
            "/turns/turn_missing/events",
            headers={"Last-Event-ID": "another:2"},
        )
        self.assertEqual(404, invalid_cursor.status_code)

        created = await self._create_turn(client, conversation_id)
        invalid_cursor = await client.get(
            created.json()["eventsUrl"],
            headers={"Last-Event-ID": "another:2"},
        )
        self.assertEqual(400, invalid_cursor.status_code)
        self.assertEqual(
            "invalid_last_event_id",
            invalid_cursor.json()["error"]["code"],
        )

    async def test_untrusted_host_and_oversized_message_are_rejected(self) -> None:
        client = await self._client(max_message_characters=4)
        untrusted = await client.get("/health", headers={"Host": "attacker.example"})
        self.assertEqual(400, untrusted.status_code)

        conversation_id = (await client.post("/conversations")).json()["id"]
        oversized = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "oversized"},
            json={"content": "12345"},
        )
        self.assertEqual(413, oversized.status_code)
        self.assertEqual("message_too_large", oversized.json()["error"]["code"])
