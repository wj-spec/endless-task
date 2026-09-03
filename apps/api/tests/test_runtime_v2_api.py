from __future__ import annotations

import asyncio
import tempfile
import unittest
from time import perf_counter
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.fake_provider import FakeProvider
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    LaneKind,
    MemoryScope,
    RunStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2 import RuntimeV2MigrationService
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)
from tests.fixtures.workspace_client import create_bound_conversation


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        cancellation_token.raise_if_cancelled()
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event


class ApprovalRequiredTool:
    definition = ToolDefinition(
        name="read_file",
        description="读取测试文件",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=1.0,
    )

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        del cancellation_token
        return ToolResult(tool_call_id=call.id, content="file content")

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class AutoReadTool:
    definition = ToolDefinition(
        name="read_file",
        description="读取测试文件",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=1.0,
    )

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        del cancellation_token
        return ToolResult(
            tool_call_id=call.id,
            content=f"content {call.arguments['path']}",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class RuntimeV2ApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(
        self,
        *,
        provider=None,
        tool_registry=None,
    ):
        selected_provider = provider or FakeProvider(chunks=("你好", "世界"))
        settings = {
            "database_path": Path(self._temporary_directory.name) / "api.db",
            "memory_proposals_enabled": False,
            "knowledge_proposals_enabled": False,
            "artifact_proposals_enabled": False,
            "scheduler_enabled": False,
            "knowledge_decay_enabled": False,
            "notifications_enabled": False,
            "task_run_review_enabled": False,
            "heartbeat_seconds": 0.05,
        }
        app = create_app(
            settings=AppSettings(**settings),
            provider=selected_provider,
            tool_registry=tool_registry,
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

    async def test_runtime_v2_selection_api(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()

        global_status = await client.get("/api/v2/runtime")
        self.assertEqual(200, global_status.status_code)
        self.assertEqual("v2", global_status.json()["defaultRuntime"])
        self.assertFalse(global_status.json()["rollbackForced"])

        initial = await client.get(
            f"/api/v2/conversations/{conversation.id}/runtime"
        )
        self.assertEqual(200, initial.status_code)
        self.assertEqual("v2", initial.json()["effectiveRuntime"])
        self.assertTrue(initial.json()["canUseV2"])

        selected = await client.post(
            f"/api/v2/conversations/{conversation.id}/runtime",
            json={"runtime": "v2"},
        )
        self.assertEqual(200, selected.status_code)
        self.assertEqual("v2", selected.json()["effectiveRuntime"])
        self.assertEqual("conversation_override", selected.json()["reason"])

        message = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "runtime-selection"},
            json={"content": "v2 path"},
        )
        self.assertEqual(202, message.status_code)
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(
                message.json()["runId"]
            ).status
            is RunStatus.COMPLETED
        )

    async def test_runtime_v2_is_default_for_new_conversation(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()

        global_status = await client.get("/api/v2/runtime")
        conversation_status = await client.get(
            f"/api/v2/conversations/{conversation.id}/runtime"
        )
        self.assertEqual("v2", global_status.json()["defaultRuntime"])
        self.assertEqual("v2", conversation_status.json()["effectiveRuntime"])

        message = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "default-runtime-first-message"},
            json={"content": "第一条消息"},
        )
        self.assertEqual(202, message.status_code)
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(
                message.json()["runId"]
            ).status
            is RunStatus.COMPLETED
        )
        lanes = (
            await client.get(f"/api/v2/conversations/{conversation.id}/lanes")
        ).json()
        self.assertEqual(1, len(lanes["items"]))
        self.assertTrue(lanes["items"][0]["isMain"])
        self.assertEqual(lanes["items"][0]["id"], lanes["activeLaneId"])

        next_conversation = await create_bound_conversation(client)
        self.assertNotEqual(conversation.id, next_conversation["id"])
        self.assertEqual("新对话", next_conversation["title"])
        next_snapshot = await client.get(
            f"/conversations/{next_conversation['id']}"
        )
        self.assertEqual([], next_snapshot.json()["turns"])

    async def test_runtime_v2_selection_rejects_unmigrated_history(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        container.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="旧会话",
        )

        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/runtime",
            json={"runtime": "v2"},
        )
        self.assertEqual(409, response.status_code)

    async def test_message_snapshot_and_sse_stream_recover_without_duplicate_delta(
        self,
    ) -> None:
        provider = FakeProvider(chunks=("你好", "世界"), pause_after_chunks=0)
        client, app = await self._client(provider=provider)
        container = app.state.container
        conversation = container.chat_repository.create_conversation()

        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "snapshot-sse-message"},
            json={"content": "打个招呼"},
        )
        self.assertEqual(202, response.status_code)
        handle = response.json()
        await _wait_until(provider.paused.is_set)

        async def read_stream() -> list[str]:
            names: list[str] = []
            async with client.stream(
                "GET",
                f"/api/v2/conversations/{conversation.id}/events",
            ) as stream_response:
                self.assertEqual(200, stream_response.status_code)
                async for line in stream_response.aiter_lines():
                    if line.startswith("event: "):
                        names.append(line.removeprefix("event: "))
            return names

        stream_task = asyncio.create_task(read_stream())
        await asyncio.sleep(0.05)
        provider.resume()
        event_names = await stream_task

        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(handle["runId"]).status
            is RunStatus.COMPLETED
        )
        snapshot = (
            await client.get(f"/api/v2/conversations/{conversation.id}/snapshot")
        ).json()
        self.assertEqual(handle["runId"], snapshot["activeRunId"])
        self.assertEqual("completed", snapshot["runState"]["status"])
        self.assertIn("conversation.snapshot_ready", event_names)
        self.assertIn("message.updated", event_names)
        self.assertIn("run.finished", event_names)

        replay_event_names: list[str] = []
        async with client.stream(
            "GET",
            f"/api/v2/conversations/{conversation.id}/events",
            params={"after_seq": 0},
        ) as stream_response:
            async for line in stream_response.aiter_lines():
                if line.startswith("event: "):
                    replay_event_names.append(line.removeprefix("event: "))

        self.assertIn("conversation.snapshot_ready", replay_event_names)
        self.assertNotIn("message.updated", replay_event_names)

    async def test_long_run_sse_recovery_and_snapshot_baseline(self) -> None:
        responses = []
        for batch_index in range(4):
            responses.append(
                (
                    ProviderToolCall(
                        id=f"long_call_{batch_index}_a",
                        name="read_file",
                        arguments={"path": f"batch-{batch_index}-a.txt"},
                    ),
                    ProviderToolCall(
                        id=f"long_call_{batch_index}_b",
                        name="read_file",
                        arguments={"path": f"batch-{batch_index}-b.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=10,
                        output_tokens=2,
                    ),
                )
            )
        responses.append(
            (
                ProviderTextDelta("长任务完成。"),
                ProviderCompleted(
                    finish_reason="stop",
                    input_tokens=20,
                    output_tokens=5,
                ),
            )
        )
        provider = ScriptedProvider(responses)
        registry = ToolRegistry()
        registry.register(AutoReadTool())
        client, app = await self._client(
            provider=provider,
            tool_registry=registry,
        )
        container = app.state.container
        conversation = container.chat_repository.create_conversation()

        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "long-run-message"},
            json={"content": "执行长任务"},
        )
        self.assertEqual(202, response.status_code)
        handle = response.json()
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(handle["runId"]).status
            is RunStatus.COMPLETED,
            timeout=3.0,
        )
        self.assertEqual(5, len(provider.requests))

        event_names: list[str] = []
        async with client.stream(
            "GET",
            f"/api/v2/conversations/{conversation.id}/events",
            params={"after_seq": 0},
        ) as stream_response:
            self.assertEqual(200, stream_response.status_code)
            async for line in stream_response.aiter_lines():
                if line.startswith("event: "):
                    event_names.append(line.removeprefix("event: "))

        self.assertEqual(1, event_names.count("conversation.snapshot_ready"))
        persisted_events = container.runtime_v2_repository.list_product_events(
            conversation.id
        )
        self.assertGreaterEqual(len(persisted_events), 30)

        started_at = perf_counter()
        snapshots = [
            (
                await client.get(
                    f"/api/v2/conversations/{conversation.id}/snapshot"
                )
            ).json()
            for _ in range(5)
        ]
        elapsed = perf_counter() - started_at
        self.assertGreaterEqual(
            snapshots[-1]["lastEventSeq"],
            len(persisted_events),
        )
        self.assertLess(elapsed, 2.0)

    async def test_lane_event_stream_validates_conversation_global_cursor(
        self,
    ) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        main = repository.create_lane(conversation_id=conversation.id)
        branch = repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            source_lane_id=main.id,
        )
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        first = repository.append_product_event(
            conversation_id=conversation.id,
            event_type="branch.created",
            run_id=None,
            lane_id=main.id,
            source_event_id="main_cursor_event",
            occurred_at="2026-01-01T00:00:00Z",
            data={},
        )
        latest = repository.append_product_event(
            conversation_id=conversation.id,
            event_type="branch.created",
            run_id=None,
            lane_id=branch.id,
            source_event_id="branch_cursor_event",
            occurred_at="2026-01-01T00:00:01Z",
            data={},
        )
        self.assertGreater(latest.event_seq, first.event_seq)

        snapshot = (
            await client.get(
                f"/api/v2/conversations/{conversation.id}/snapshot",
                params={"lane_id": main.id},
            )
        ).json()
        self.assertEqual(latest.event_seq, snapshot["lastEventSeq"])

        async with client.stream(
            "GET",
            f"/api/v2/conversations/{conversation.id}/events",
            params={"after_seq": latest.event_seq, "lane_id": main.id},
        ) as stream_response:
            self.assertEqual(200, stream_response.status_code)
            body = "\n".join([line async for line in stream_response.aiter_lines()])
        self.assertIn("event: conversation.snapshot_ready", body)

        beyond_latest = await client.get(
            f"/api/v2/conversations/{conversation.id}/events",
            params={"after_seq": latest.event_seq + 1, "lane_id": main.id},
        )
        self.assertEqual(400, beyond_latest.status_code)
        self.assertEqual("invalid_after_seq", beyond_latest.json()["error"]["code"])

    async def test_lane_branch_lifecycle_and_promote_api(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        main = repository.create_lane(conversation_id=conversation.id)
        question = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        base = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "主线回复"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=question.id,
        )
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )

        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/lanes",
            json={
                "kind": "persistent_branch",
                "sourceLaneId": main.id,
                "baseEntryId": base.id,
                "displayName": "对照分支",
            },
        )

        self.assertEqual(201, response.status_code)
        lane = response.json()["lane"]
        self.assertEqual("persistent_branch", lane["kind"])
        self.assertEqual("active", lane["status"])
        self.assertFalse(lane["archived"])
        self.assertEqual("对照分支", lane["displayName"])
        self.assertEqual("对照分支", lane["title"])
        self.assertEqual(main.id, lane["sourceLaneId"])
        self.assertEqual(base.id, lane["baseEntryId"])

        listed = await client.get(
            f"/api/v2/conversations/{conversation.id}/lanes"
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual(main.id, listed.json()["activeLaneId"])
        self.assertEqual(2, len(listed.json()["items"]))

        renamed = await client.patch(
            f"/api/v2/lanes/{lane['id']}",
            json={"displayName": "  已命名分支  "},
        )
        self.assertEqual(200, renamed.status_code)
        self.assertEqual("已命名分支", renamed.json()["lane"]["displayName"])
        self.assertEqual("已命名分支", renamed.json()["lane"]["title"])

        archived = await client.post(f"/api/v2/lanes/{lane['id']}/archive")
        self.assertEqual(200, archived.status_code)
        self.assertEqual(
            ("archived",),
            tuple(item["status"] for item in archived.json()["items"]),
        )
        self.assertTrue(all(item["archived"] for item in archived.json()["items"]))

        active_lanes = await client.get(
            f"/api/v2/conversations/{conversation.id}/lanes"
        )
        self.assertEqual(200, active_lanes.status_code)
        self.assertNotIn(
            lane["id"],
            tuple(item["id"] for item in active_lanes.json()["items"]),
        )

        all_lanes = await client.get(
            f"/api/v2/conversations/{conversation.id}/lanes",
            params={"includeArchived": "true"},
        )
        self.assertEqual(200, all_lanes.status_code)
        self.assertIn(
            lane["id"],
            tuple(item["id"] for item in all_lanes.json()["items"]),
        )

        restored = await client.post(f"/api/v2/lanes/{lane['id']}/restore")
        self.assertEqual(200, restored.status_code)
        self.assertEqual(
            ("active",),
            tuple(item["status"] for item in restored.json()["items"]),
        )

        repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane["id"],
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "分支回复"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        branch_snapshot = await client.get(
            f"/api/v2/conversations/{conversation.id}/snapshot"
            f"?lane_id={lane['id']}"
        )
        main_snapshot = await client.get(
            f"/api/v2/conversations/{conversation.id}/snapshot"
            f"?lane_id={main.id}"
        )
        self.assertEqual(200, branch_snapshot.status_code)
        self.assertEqual(200, main_snapshot.status_code)
        self.assertEqual(lane["id"], branch_snapshot.json()["activeLaneId"])
        self.assertEqual(
            ("主线历史", "主线回复", "分支回复"),
            tuple(
                entry["data"]["content"]
                for entry in branch_snapshot.json()["entries"]
                if entry["type"] == "assistant_message"
                or entry["type"] == "user_message"
            ),
        )
        self.assertEqual(
            ("主线历史", "主线回复"),
            tuple(
                entry["data"]["content"]
                for entry in main_snapshot.json()["entries"]
                if entry["type"] == "assistant_message"
                or entry["type"] == "user_message"
            ),
        )

        promoted = await client.post(f"/api/v2/lanes/{lane['id']}/promote")
        self.assertEqual(200, promoted.status_code)
        self.assertEqual(lane["id"], promoted.json()["activeLaneId"])
        self.assertEqual(main.id, promoted.json()["previousMainLane"]["id"])
        branch_events = [
            event
            for event in container.runtime_v2_gateway.project_events(
                conversation.id
            )
            if event.event_type.startswith("branch.")
        ]
        self.assertEqual(
            (
                "branch.created",
                "branch.renamed",
                "branch.archived",
                "branch.restored",
                "branch.promoted",
            ),
            tuple(event.event_type for event in branch_events),
        )
        rename_event = next(
            event for event in branch_events if event.event_type == "branch.renamed"
        )
        self.assertEqual("已命名分支", rename_event.data["displayName"])
        self.assertEqual("对照分支", rename_event.data["previousDisplayName"])

    async def test_temporary_conversation_lifecycle_api_projects_events(
        self,
    ) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        main = repository.create_lane(conversation_id=conversation.id)
        base = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=main.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "主线历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=main.id,
        )
        visible_branch = repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base.id,
        )
        branch_leaf = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=visible_branch.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "仅分支可见的历史"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )

        created = await client.post(
            f"/api/v2/conversations/{conversation.id}/temporary-conversations",
            json={
                "sourceLaneId": visible_branch.id,
                "sourceLeafEntryId": branch_leaf.id,
                "title": "临时探索",
            },
        )
        self.assertEqual(201, created.status_code)
        temporary = created.json()["conversation"]
        temporary_lane = created.json()["lane"]
        self.assertEqual("ephemeral", temporary["kind"])
        self.assertEqual("temporary", temporary_lane["kind"])
        temporary_snapshot = await client.get(
            f"/api/v2/conversations/{temporary['id']}/snapshot",
            params={"lane_id": temporary_lane["id"]},
        )
        self.assertEqual(200, temporary_snapshot.status_code)
        self.assertEqual(
            ("主线历史",),
            tuple(
                entry["data"]["content"]
                for entry in temporary_snapshot.json()["entries"]
                if entry["type"] in {"user_message", "assistant_message"}
            ),
        )
        self.assertTrue(
            all(
                entry["inherited"]
                for entry in temporary_snapshot.json()["entries"]
            )
        )

        created_events = [
            event
            for event in container.runtime_v2_gateway.project_events(
                temporary["id"]
            )
            if event.event_type.startswith("temporary_conversation.")
        ]
        self.assertEqual(
            ("temporary_conversation.created",),
            tuple(event.event_type for event in created_events),
        )
        self.assertEqual(
            conversation.id,
            created_events[0].data["sourceConversationId"],
        )
        self.assertEqual(main.id, created_events[0].data["sourceLaneId"])

        promoted = await client.post(
            f"/api/v2/temporary-conversations/{temporary['id']}/promote"
        )
        self.assertEqual(200, promoted.status_code)
        self.assertEqual("normal", promoted.json()["conversation"]["kind"])
        promoted_events = [
            event
            for event in container.runtime_v2_gateway.project_events(
                temporary["id"]
            )
            if event.event_type.startswith("temporary_conversation.")
        ]
        self.assertEqual(
            (
                "temporary_conversation.created",
                "temporary_conversation.promoted",
            ),
            tuple(event.event_type for event in promoted_events),
        )
        self.assertEqual(
            temporary["id"],
            promoted_events[-1].data["temporaryConversationId"],
        )

        disposable = await client.post(
            f"/api/v2/conversations/{conversation.id}/temporary-conversations",
            json={
                "sourceLaneId": main.id,
                "sourceLeafEntryId": base.id,
                "title": "一次性探索",
            },
        )
        self.assertEqual(201, disposable.status_code)
        disposable_id = disposable.json()["conversation"]["id"]
        deleted = await client.delete(
            f"/api/v2/temporary-conversations/{disposable_id}"
        )
        self.assertEqual(204, deleted.status_code)

        source_events = [
            event
            for event in container.runtime_v2_gateway.project_events(
                conversation.id
            )
            if event.event_type.startswith("temporary_conversation.")
        ]
        self.assertEqual(
            ("temporary_conversation.deleted",),
            tuple(event.event_type for event in source_events),
        )
        self.assertEqual(
            disposable_id,
            source_events[0].data["temporaryConversationId"],
        )

    async def test_run_variant_regenerate_list_and_select_api(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("第一版"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=7,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("第二版"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=8,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        client, app = await self._client(provider=provider)
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "run-regenerate-message"},
            json={"content": "重新生成这个问题"},
        )
        self.assertEqual(202, response.status_code)
        first = response.json()
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(first["runId"]).status
            is RunStatus.COMPLETED
        )

        regenerated = await client.post(
            f"/api/v2/runs/{first['runId']}/regenerate"
        )
        self.assertEqual(202, regenerated.status_code)
        body = regenerated.json()
        self.assertEqual(first["runId"], body["oldRunId"])
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(body["newRunId"]).status
            is RunStatus.COMPLETED
        )

        variants = await client.get(f"/api/v2/runs/{body['newRunId']}/variants")
        self.assertEqual(200, variants.status_code)
        items = variants.json()["items"]
        self.assertEqual(2, len(items))
        self.assertEqual(
            (False, True),
            tuple(item["isActiveVariant"] for item in items),
        )

        selected = await client.post(f"/api/v2/runs/{first['runId']}/select")
        self.assertEqual(200, selected.status_code)
        self.assertTrue(selected.json()["isActiveVariant"])
        snapshot = await client.get(
            f"/api/v2/conversations/{conversation.id}/snapshot"
        )
        self.assertEqual(first["runId"], snapshot.json()["activeRunVariantId"])

    async def test_runtime_v2_memory_scope_and_promotion_api(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        lane = repository.create_lane(conversation_id=conversation.id)
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
        )

        created = await client.post(
            f"/api/v2/conversations/{conversation.id}/memories",
            json={"kind": "fact", "content": "分支内产生的记忆"},
        )
        self.assertEqual(201, created.status_code)
        memory = created.json()["memory"]
        self.assertEqual("branch", memory["scope"])
        self.assertEqual(lane.id, memory["laneId"])

        listed = await client.get(
            f"/api/v2/conversations/{conversation.id}/memories",
            params={"lane_id": lane.id},
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual(
            ("分支内产生的记忆",),
            tuple(item["content"] for item in listed.json()["items"]),
        )

        promotion_response = await client.post(
            f"/api/v2/memories/{memory['id']}/promotions",
            json={"targetScope": "conversation_tree"},
        )
        self.assertEqual(201, promotion_response.status_code)
        promotion = promotion_response.json()["promotion"]
        self.assertEqual("conversation_tree", promotion["targetScope"])
        self.assertEqual("pending", promotion["status"])

        pending = await client.get(
            f"/api/v2/conversations/{conversation.id}/memory-promotions"
        )
        self.assertEqual(200, pending.status_code)
        self.assertEqual(1, len(pending.json()["items"]))

        resolved = await client.post(
            f"/api/v2/memory-promotions/{promotion['id']}/resolve",
            json={"decision": "accept"},
        )
        self.assertEqual(200, resolved.status_code)
        self.assertEqual("accepted", resolved.json()["promotion"]["status"])
        self.assertIsNotNone(resolved.json()["memory"])
        self.assertEqual(
            "conversation_tree",
            resolved.json()["memory"]["scope"],
        )
        self.assertEqual(
            ("memory.proposal_created", "memory.scope_changed"),
            tuple(
                event.event_type
                for event in container.runtime_v2_gateway.project_events(
                    conversation.id
                )
                if event.event_type.startswith("memory.")
            ),
        )

    async def test_runtime_v2_memory_promotion_conflict_api(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        lane = repository.create_lane(conversation_id=conversation.id)
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
        )
        existing = container.runtime_v2_memory_repository.create_memory(
            scope=MemoryScope.CONVERSATION_TREE,
            kind="preference",
            content="用户偏好结构化回答",
            conversation_id=conversation.id,
        )

        created = await client.post(
            f"/api/v2/conversations/{conversation.id}/memories",
            json={"kind": "fact", "content": "用户偏好结构化回答"},
        )
        self.assertEqual(201, created.status_code)
        memory = created.json()["memory"]

        promotion_response = await client.post(
            f"/api/v2/memories/{memory['id']}/promotions",
            json={"targetScope": "conversation_tree"},
        )
        self.assertEqual(201, promotion_response.status_code)
        promotion = promotion_response.json()["promotion"]
        self.assertEqual(existing.id, promotion["conflictMemoryId"])

        resolved = await client.post(
            f"/api/v2/memory-promotions/{promotion['id']}/resolve",
            json={"decision": "accept"},
        )
        self.assertEqual(200, resolved.status_code)
        self.assertEqual(existing.id, resolved.json()["promotion"]["resolvedMemoryId"])
        self.assertEqual(existing.id, resolved.json()["memory"]["id"])

    async def test_approval_resolve_api_completes_run(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=10,
                        output_tokens=2,
                    ),
                ),
                (
                    ProviderTextDelta("已读取文件。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=12,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(ApprovalRequiredTool())
        client, app = await self._client(
            provider=provider,
            tool_registry=registry,
        )
        container = app.state.container
        conversation = container.chat_repository.create_conversation()

        response = await client.post(
            f"/api/v2/conversations/{conversation.id}/messages",
            headers={"Idempotency-Key": "approval-message"},
            json={"content": "读取文件"},
        )
        self.assertEqual(202, response.status_code)
        run_id = response.json()["runId"]
        snapshot = {}
        for _ in range(200):
            snapshot = (
                await client.get(f"/api/v2/conversations/{conversation.id}/snapshot")
            ).json()
            if snapshot.get("pendingApprovals"):
                break
            await asyncio.sleep(0.005)
        self.assertEqual(1, len(snapshot["pendingApprovals"]), snapshot)
        approval = snapshot["pendingApprovals"][0]
        self.assertNotIn("metadata", approval)

        resolved = await client.post(
            f"/api/v2/approvals/{approval['id']}",
            json={"decision": "approve"},
        )
        self.assertEqual(200, resolved.status_code)
        self.assertTrue(resolved.json()["resolved"])
        await _wait_until(
            lambda: container.runtime_v2_repository.get_run(run_id).status
            is RunStatus.COMPLETED
        )

        events = container.runtime_v2_gateway.project_events(conversation.id)
        approval_events = [
            event for event in events if event.event_type.startswith("approval.")
        ]
        self.assertEqual(
            ("approval.requested", "approval.resolved"),
            tuple(event.event_type for event in approval_events),
        )

    async def test_recovery_report_api_exposes_interrupted_run(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        lane = repository.create_lane(conversation_id=conversation.id)
        entry = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "中断的问题"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
            is_active_variant=True,
        )
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        repository.start_run(run.id)

        response = await client.get(
            f"/api/v2/conversations/{conversation.id}/recovery"
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, len(body["interruptedRuns"]))
        self.assertEqual(run.id, body["interruptedRuns"][0]["runId"])
        self.assertEqual("running", body["interruptedRuns"][0]["status"])

        response = await client.post(
            f"/api/v2/runs/{run.id}/recovery",
            json={"action": "mark_failed"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "runId": run.id,
                "action": "mark_failed",
                "laneId": lane.id,
                "newRunId": None,
            },
            response.json(),
        )
        self.assertEqual(
            RunStatus.FAILED,
            repository.get_run(run.id).status,
        )

        report = await client.get(
            f"/api/v2/conversations/{conversation.id}/recovery"
        )
        self.assertEqual([], report.json()["interruptedRuns"])

    async def test_recovery_retry_api_starts_new_run(self) -> None:
        client, app = await self._client()
        container = app.state.container
        conversation = container.chat_repository.create_conversation()
        repository = container.runtime_v2_repository
        lane = repository.create_lane(conversation_id=conversation.id)
        entry = repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "中断后重试"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=entry.id,
            is_active_variant=True,
        )
        repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        repository.start_run(run.id)

        response = await client.post(
            f"/api/v2/runs/{run.id}/recovery",
            json={"action": "retry"},
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("retry", body["action"])
        self.assertIsNotNone(body["newRunId"])
        await _wait_until(
            lambda: repository.get_run(body["newRunId"]).status
            is RunStatus.COMPLETED
        )

        old_run = repository.get_run(run.id)
        new_run = repository.get_run(body["newRunId"])
        self.assertEqual(RunStatus.FAILED, old_run.status)
        self.assertFalse(old_run.is_active_variant)
        self.assertEqual(RunStatus.COMPLETED, new_run.status)
        self.assertTrue(new_run.is_active_variant)
        self.assertEqual(old_run.sibling_group_id, new_run.sibling_group_id)


if __name__ == "__main__":
    unittest.main()