from __future__ import annotations

import asyncio
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
    setting_overrides.setdefault("runtime", "v1")
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
        yield client
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
    async def _wait_for_terminal(client: httpx.AsyncClient, turn_id: str):
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            payload = response.json()
            if payload["turnStatus"] in {"completed", "failed", "cancelled"}:
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("Turn did not reach a terminal state")

    @staticmethod
    async def _submit(
        client: httpx.AsyncClient,
        conversation_id: str,
        ordinal: int,
        content: str,
    ):
        response = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": f"request-{ordinal}"},
            json={"content": content},
        )
        if response.status_code != 202:
            raise AssertionError(response.text)
        return response.json()

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

    @staticmethod
    async def _event_types(client: httpx.AsyncClient, turn_id: str) -> list[str]:
        response = await client.get(f"/turns/{turn_id}/events")
        return [
            line.split(": ", 1)[1]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]

    async def test_plain_question_is_answered_without_tool_activity(self) -> None:
        provider = GateProvider(
            (
                (
                    ProviderTextDelta("你好！有什么可以帮你？"),
                    ProviderCompleted("stop"),
                ),
            )
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            created = await self._submit(client, conversation_id, 1, "你好")
            terminal = await self._wait_for_terminal(client, created["turnId"])

            self.assertEqual("completed", terminal["turnStatus"])
            self.assertEqual("你好！有什么可以帮你？", terminal["content"])
            self.assertEqual([], terminal["activities"])
            self.assertEqual(1, len(provider.requests))
            # 工具定义可用，但模型不调用时不得产生任何工具状态。
            # 会话现在归属已绑定工作区，工作区工具（fs/shell）也一并注入。
            self.assertEqual(
                (
                    "delete_workspace_file",
                    "list_workspace_dir",
                    "read_artifact",
                    "read_skill_file",
                    "read_text_file",
                    "read_workspace_file",
                    "run_shell",
                    "write_workspace_file",
                ),
                tuple(tool.name for tool in provider.requests[0].tools),
            )
            event_types = await self._event_types(client, created["turnId"])
            self.assertNotIn("activity.started", event_types)
            self.assertEqual(1, event_types.count("turn.completed"))

    async def test_file_question_automatically_selects_file_tool(self) -> None:
        provider = GateProvider()
        async with local_client(self.database_path, provider) as client:
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
            terminal = await self._wait_for_terminal(client, created["turnId"])

            self.assertEqual("completed", terminal["turnStatus"])
            self.assertEqual("这份文档的主题是本地优先。", terminal["content"])
            # 工具结果作为不可信数据返回给了模型。
            tool_message = provider.requests[1].messages[-1]
            self.assertEqual("tool", tool_message.role)
            self.assertEqual("call_1", tool_message.tool_call_id)
            self.assertIn("本项目坚持本地优先。", tool_message.content)
            # Activity 只显示自然状态，不暴露原始参数。
            self.assertEqual(1, len(terminal["activities"]))
            activity = terminal["activities"][0]
            self.assertEqual("completed", activity["status"])
            self.assertEqual("已读取 需求文档.txt", activity["message"])

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
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            created = await self._submit(client, conversation_id, 1, "帮我读一下文件")
            terminal = await self._wait_for_terminal(client, created["turnId"])

            self.assertEqual("completed", terminal["turnStatus"])
            self.assertIn("哪份文件", terminal["content"])
            self.assertEqual([], terminal["activities"])
            self.assertEqual(1, len(provider.requests))

    async def test_tool_failure_is_understood_and_explained_by_assistant(self) -> None:
        provider = GateProvider()
        async with local_client(self.database_path, provider) as client:
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
            terminal = await self._wait_for_terminal(client, created["turnId"])

            # 工具失败由 Assistant 解释，而不是让整个 Turn 失败。
            self.assertEqual("completed", terminal["turnStatus"])
            self.assertEqual(
                "文件只有两行，无法从第 999 行读取；需要的话我可以从头读。",
                terminal["content"],
            )
            failure_feedback = provider.requests[1].messages[-1]
            self.assertEqual("tool", failure_feedback.role)
            self.assertIn("file_line_out_of_range", failure_feedback.content)
            self.assertEqual(1, len(terminal["activities"]))
            activity = terminal["activities"][0]
            self.assertEqual("failed", activity["status"])
            self.assertEqual("读取 需求文档.txt 失败，可以重试", activity["message"])

    async def test_api_exposes_no_chat_work_mode(self) -> None:
        provider = GateProvider(
            ((ProviderTextDelta("好的"), ProviderCompleted("stop")),)
        )
        async with local_client(self.database_path, provider) as client:
            conversation_id = (await create_bound_conversation(client))["id"]

            rejected = await client.post(
                f"/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "mode-1"},
                json={"content": "你好", "mode": "work"},
            )
            self.assertEqual(400, rejected.status_code)
            self.assertEqual("invalid_request", rejected.json()["error"]["code"])

            created = await self._submit(client, conversation_id, 1, "你好")
            self.assertNotIn("mode", created)
            terminal = await self._wait_for_terminal(client, created["turnId"])
            self.assertNotIn("mode", terminal)
            self.assertEqual("completed", terminal["turnStatus"])

    async def test_agent_loop_limit_stops_turn_safely(self) -> None:
        provider = GateProvider()
        async with local_client(
            self.database_path,
            provider,
            max_agent_iterations=2,
        ) as client:
            conversation_id = (await create_bound_conversation(client))["id"]
            file_id = await self._upload_file(client, conversation_id)
            provider.responses = (
                (
                    read_file_call("call_1", file_id, start_line=1),
                    ProviderCompleted("tool_calls"),
                ),
                (
                    read_file_call("call_2", file_id, start_line=2),
                    ProviderCompleted("tool_calls"),
                ),
                (ProviderTextDelta("不应到达这里"), ProviderCompleted("stop")),
            )

            created = await self._submit(client, conversation_id, 1, "一直读下去")
            terminal = await self._wait_for_terminal(client, created["turnId"])

            self.assertEqual("failed", terminal["turnStatus"])
            self.assertEqual("tool_loop_limit", terminal["error"]["code"])
            self.assertEqual(
                "工具调用次数达到上限，请缩小请求范围后重试。",
                terminal["error"]["message"],
            )
            self.assertFalse(terminal["error"]["retryable"])
            # 达到上限后安全停止：只执行了一次工具调用，终态事件唯一。
            self.assertEqual(1, len(terminal["activities"]))
            self.assertEqual("completed", terminal["activities"][0]["status"])
            event_types = await self._event_types(client, created["turnId"])
            self.assertEqual(1, event_types.count("turn.failed"))
            self.assertEqual(2, len(provider.requests))


if __name__ == "__main__":
    unittest.main()