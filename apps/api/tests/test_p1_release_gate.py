from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from tests.fixtures.v2_client import (
    assistant_text,
    run_snapshot,
    send_message,
    wait_for_run_terminal,
)
from tests.fixtures.workspace_client import create_bound_conversation


class GateProvider:
    """Scripted provider whose response sequence can be prepared after setup."""

    name = "gate"

    def __init__(self, responses=()) -> None:
        self.responses = list(responses)
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        for event in self.responses[len(self.requests) - 1]:
            cancellation_token.raise_if_cancelled()
            yield event


def read_file_call(call_id: str, file_id: str, **extra_arguments) -> ProviderToolCall:
    arguments = {"file_id": file_id}
    arguments.update(extra_arguments)
    return ProviderToolCall(
        id=call_id,
        name="read_text_file",
        arguments=arguments,
    )


@asynccontextmanager
async def local_client(database_path: Path, provider, **setting_overrides):
    setting_overrides.setdefault("memory_proposals_enabled", False)
    setting_overrides.setdefault("knowledge_proposals_enabled", False)
    app = create_app(
        settings=AppSettings(database_path=database_path, **setting_overrides),
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


class P1ReleaseGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "release.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(client: httpx.AsyncClient, app, handle: dict):
        await wait_for_run_terminal(app.state.container, handle["runId"])
        snapshot = await run_snapshot(client, handle["conversationId"])
        return snapshot

    @staticmethod
    async def _submit(
        client: httpx.AsyncClient,
        conversation_id: str,
        ordinal: int,
        content: str,
    ) -> dict:
        handle = await send_message(
            client,
            conversation_id,
            content,
            idempotency_key=f"request-{ordinal}",
        )
        return handle

    @staticmethod
    async def _upload_file(
        client: httpx.AsyncClient,
        conversation_id: str,
        *,
        filename: str = "需求文档.txt",
        content: str = "本项目坚持本地优先。\n聊天是产品本体。",
    ) -> str:
        response = await client.post(
            f"/conversations/{conversation_id}/files",
            params={"filename": filename},
            content=content.encode("utf-8"),
            headers={"content-type": "text/plain"},
        )
        if response.status_code != 201:
            raise AssertionError(response.text)
        return response.json()["id"]

    async def test_plain_question_is_answered_without_tool_activity(self) -> None:
        provider = GateProvider(
            (
                (
                    ProviderTextDelta("你好！有什么可以帮你？"),
                    ProviderCompleted("stop"),
                ),
            )
        )
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            created = await self._submit(client, conversation_id, 1, "你好")
            snapshot = await self._wait_for_terminal(client, app, created)

            self.assertEqual("completed", snapshot["runState"]["status"])
            self.assertEqual("你好！有什么可以帮你？", assistant_text(snapshot))
            self.assertEqual([], snapshot["toolStates"])
            self.assertEqual(1, len(provider.requests))
            # 工具定义可用，但模型不调用时不得产生任何工具状态。
            # 会话现在归属已绑定工作区，工作区工具（fs/shell）也一并注入；
            # delegation 默认 readonly 后，bound 会话经 agent.delegate 授权
            # 门额外可见 spawn/query/cancel_agent（06 §29，M4A 默认开启）。
            self.assertEqual(
                (
                    "cancel_agent",
                    "delete_workspace_file",
                    "list_workspace_dir",
                    "query_agent",
                    "read_artifact",
                    "read_skill_file",
                    "read_text_file",
                    "read_workspace_file",
                    "run_shell",
                    "spawn_agent",
                    "update_plan",
                    "write_workspace_file",
                ),
                tuple(tool.name for tool in provider.requests[0].tools),
            )

    async def test_file_question_automatically_selects_file_tool(self) -> None:
        provider = GateProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            file_id = await self._upload_file(client, conversation_id)
            provider.responses = (
                (
                    read_file_call("call_1", file_id),
                    ProviderCompleted("tool_calls"),
                ),
                (
                    ProviderTextDelta("这份文档的主题是本地优先。"),
                    ProviderCompleted("stop"),
                ),
            )

            created = await self._submit(
                client,
                conversation_id,
                1,
                "帮我总结这份文档的主题",
            )
            snapshot = await self._wait_for_terminal(client, app, created)

            self.assertEqual("completed", snapshot["runState"]["status"])
            self.assertEqual("这份文档的主题是本地优先。", assistant_text(snapshot))
            # 工具结果作为不可信数据返回给了模型。
            tool_message = provider.requests[1].messages[-1]
            self.assertEqual("tool", tool_message.role)
            self.assertEqual("call_1", tool_message.tool_call_id)
            self.assertIn("本项目坚持本地优先。", tool_message.content)
            self.assertEqual(1, len(snapshot["toolStates"]))
            self.assertEqual("completed", snapshot["toolStates"][0]["status"])
            self.assertEqual("read_text_file", snapshot["toolStates"][0]["toolName"])

    async def test_insufficient_information_asks_follow_up_without_tool_state(
        self,
    ) -> None:
        provider = GateProvider(
            (
                (
                    ProviderTextDelta("你希望我读取哪份文件？请先上传或说明文件名。"),
                    ProviderCompleted("stop"),
                ),
            )
        )
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            created = await self._submit(client, conversation_id, 1, "帮我读一下文件")
            snapshot = await self._wait_for_terminal(client, app, created)

            self.assertEqual("completed", snapshot["runState"]["status"])
            self.assertIn("哪份文件", assistant_text(snapshot))
            self.assertEqual([], snapshot["toolStates"])
            self.assertEqual(1, len(provider.requests))

    async def test_tool_failure_is_understood_and_explained_by_assistant(self) -> None:
        provider = GateProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            file_id = await self._upload_file(client, conversation_id)
            provider.responses = (
                (
                    read_file_call("call_1", file_id, start_line=999),
                    ProviderCompleted("tool_calls"),
                ),
                (
                    ProviderTextDelta("文件只有两行，无法从第 999 行读取；需要的话我可以从头读。"),
                    ProviderCompleted("stop"),
                ),
            )

            created = await self._submit(
                client,
                conversation_id,
                1,
                "读取文档第 999 行开始的内容",
            )
            snapshot = await self._wait_for_terminal(client, app, created)

            # 工具失败由 Assistant 解释，而不是让整个 Run 失败。
            self.assertEqual("completed", snapshot["runState"]["status"])
            self.assertEqual(
                "文件只有两行，无法从第 999 行读取；需要的话我可以从头读。",
                assistant_text(snapshot),
            )
            failure_feedback = provider.requests[1].messages[-1]
            self.assertEqual("tool", failure_feedback.role)
            self.assertIn("line_out_of_range", failure_feedback.content)
            self.assertEqual(1, len(snapshot["toolStates"]))
            self.assertEqual("failed", snapshot["toolStates"][0]["status"])
            self.assertEqual("read_text_file", snapshot["toolStates"][0]["toolName"])


if __name__ == "__main__":
    unittest.main()
