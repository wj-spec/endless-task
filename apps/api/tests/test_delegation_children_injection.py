"""运行开始时的 <delegated-children> 注入：真的进了模型请求，且只对终态盖章。

纯函数的筛选/渲染由 `test_delegation_child_context.py` 覆盖；这里覆盖的是**接线**：
块出现在请求的哪条消息里、终态盖章/运行中不盖章、以及注入失败绝不让运行起不来。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.delegation.child_run_store import ChildRunState
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


class RecordingProvider:
    """记录每次请求的 messages，用于断言注入内容。"""

    name = "recording"

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        yield ProviderTextDelta("好的。")
        yield ProviderCompleted(finish_reason="stop")


def _messages_text(provider: RecordingProvider) -> str:
    parts: list[str] = []
    for request in provider.requests:
        for message in getattr(request, "messages", ()):
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(f"[{getattr(message, 'role', '')}]{content}")
    return "\n".join(parts)


def _reserve_child(repository: Any, *, child_run_id: str, conversation_id: str) -> Any:
    return repository.reserve_child_run(
        child_run_id=child_run_id,
        parent_run_id="run_parent",
        parent_tool_call_id=f"call_{child_run_id}",
        parent_conversation_id=conversation_id,
        depth=1,
        capability_profile="subagent_readonly",
        workspace_mode="read_only_shared",
        timeout_seconds=120.0,
        task="总结仓库测试现状",
    )


class DelegatedChildrenInjectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.provider = RecordingProvider()

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=self.data_dir / "app.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=self.provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        return app, lifespan, client

    async def _conversation(self, client: httpx.AsyncClient, *, name: str) -> str:
        workspace = await client.post(
            "/workspaces", json={"name": name, "rootPath": str(self._temp.name)}
        )
        workspace_id = workspace.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    async def test_terminal_child_is_injected_as_user_message_and_stamped(self) -> None:
        app, lifespan, client = await self._client()
        try:
            conversation_id = await self._conversation(client, name="注入区")
            repository = app.state.container.delegation_child_run_repository
            _reserve_child(
                repository, child_run_id="child_done", conversation_id=conversation_id
            )
            repository.mark_child_terminal(
                "child_done",
                state=ChildRunState.COMPLETED,
                summary="仓库共 2077 个测试。",
            )

            handle = await send_message(
                client, conversation_id, "看看子任务", idempotency_key="children-1"
            )
            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)

            text = _messages_text(self.provider)
            self.assertIn("<delegated-children>", text)
            self.assertIn("[user]<delegated-children>", text)  # 不获得 system 级权威
            self.assertIn("child_done", text)
            self.assertIn("仓库共 2077 个测试。", text)
            # 终态已露面 → 盖章（下一轮就不再重复注入）。
            self.assertIsNotNone(repository.get_child_run("child_done").injected_at)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_running_and_queued_children_are_listed_but_not_stamped(self) -> None:
        app, lifespan, client = await self._client()
        try:
            conversation_id = await self._conversation(client, name="注入区2")
            repository = app.state.container.delegation_child_run_repository
            # 一个已经在内核里跑（started_at 非空），一个还在并发闸门外排队。
            _reserve_child(
                repository, child_run_id="child_running", conversation_id=conversation_id
            )
            _reserve_child(
                repository, child_run_id="child_queued", conversation_id=conversation_id
            )
            repository.mark_child_started("child_running")

            handle = await send_message(
                client, conversation_id, "子任务在跑吗", idempotency_key="children-2"
            )
            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)

            text = _messages_text(self.provider)
            self.assertIn("child_running", text)
            self.assertIn("运行中", text)
            self.assertIn("child_queued", text)
            self.assertIn("排队中", text)
            # 运行中的行盖章会让它终态后再也不被注入：只能列，不能盖。
            self.assertIsNone(repository.get_child_run("child_running").injected_at)
            self.assertIsNone(repository.get_child_run("child_queued").injected_at)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_conversation_without_children_gets_no_block(self) -> None:
        app, lifespan, client = await self._client()
        try:
            conversation_id = await self._conversation(client, name="注入区3")
            handle = await send_message(
                client, conversation_id, "没有子任务", idempotency_key="children-3"
            )
            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)
            self.assertNotIn("<delegated-children>", _messages_text(self.provider))
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_render_failure_never_breaks_the_run(self) -> None:
        app, lifespan, client = await self._client()
        try:
            conversation_id = await self._conversation(client, name="注入区4")
            repository = app.state.container.delegation_child_run_repository

            def _boom(_conversation_id: str):
                raise RuntimeError("查询子运行炸了")

            repository.list_child_runs_for_conversation = _boom  # type: ignore[method-assign]

            with self.assertLogs("endless_task.api.wiring", level="ERROR") as captured:
                handle = await send_message(
                    client, conversation_id, "渲染失败也要能跑", idempotency_key="children-4"
                )
                status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)
            self.assertIn(
                "组装 <delegated-children> 注入块失败", "\n".join(captured.output)
            )
            self.assertNotIn("<delegated-children>", _messages_text(self.provider))
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_marking_failure_never_breaks_the_run(self) -> None:
        app, lifespan, client = await self._client()
        try:
            conversation_id = await self._conversation(client, name="注入区5")
            repository = app.state.container.delegation_child_run_repository
            _reserve_child(
                repository, child_run_id="child_stamp", conversation_id=conversation_id
            )
            repository.mark_child_terminal(
                "child_stamp", state=ChildRunState.COMPLETED, summary="结论在此"
            )

            def _boom(_child_run_id: str):
                raise RuntimeError("盖章炸了")

            repository.mark_child_injected = _boom  # type: ignore[method-assign]

            with self.assertLogs("endless_task.api.wiring", level="ERROR") as captured:
                handle = await send_message(
                    client, conversation_id, "盖章失败也要能跑", idempotency_key="children-5"
                )
                status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)
            self.assertIn("标记子运行已注入失败", "\n".join(captured.output))
            # 盖章失败只影响"下次是否重复注入"，块本身照常给模型。
            self.assertIn("<delegated-children>", _messages_text(self.provider))
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
