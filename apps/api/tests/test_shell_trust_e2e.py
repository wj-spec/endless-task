"""S8 端到端：只读信任策略在真实 v2 run 里的审批行为。

- 信任关闭（默认）：run_shell 停在等待审批（与历史行为一致）。
- 信任开启：只读命令自动放行，命令正常执行并完成。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


class SwappableProvider:
    name = "swappable"

    def __init__(self, inner) -> None:
        self._inner = inner

    async def stream(self, request, cancellation_token):
        async for item in self._inner.stream(request, cancellation_token):
            yield item


class ScriptedShellProvider:
    name = "scripted-shell"

    def __init__(self, command: str) -> None:
        self._command = command
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_shell_1",
                name="run_shell",
                arguments={"command": self._command},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(finish_reason="stop")


class ShellTrustE2ETest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name) / "ws"
        self._root.mkdir()

    async def asyncTearDown(self) -> None:
        for client, lifespan in getattr(self, "_apps", []):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._tmp.cleanup()

    async def _app(self, provider, *, env: dict[str, str]) -> tuple[httpx.AsyncClient, object]:
        index = len(getattr(self, "_apps", []))
        swappable = SwappableProvider(provider)
        with mock.patch.dict(os.environ, env, clear=False):
            app = create_app(
                settings=AppSettings(
                    database_path=Path(self._tmp.name) / f"trust-{index}.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                ),
                provider=swappable,
            )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        apps = getattr(self, "_apps", [])
        apps.append((client, lifespan))
        self._apps = apps
        return client, app

    async def _submit(self, client: httpx.AsyncClient, key: str) -> tuple[str, str]:
        created = await client.post(
            "/workspaces",
            json={"name": f"Trust-{key}", "rootPath": str(self._root)},
        )
        workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        conversation_id = conversation.json()["id"]
        handle = await send_message(
            client, conversation_id, "开始", idempotency_key=key
        )
        return conversation_id, handle["runId"]

    async def _wait_status(self, container, run_id: str, expected: RunStatus) -> RunStatus:
        deadline = asyncio.get_running_loop().time() + 4.0
        while asyncio.get_running_loop().time() < deadline:
            status = container.runtime_v2_repository.get_run(run_id).status
            if status == expected:
                return status
            await asyncio.sleep(0.005)
        raise AssertionError(
            f"run {run_id} did not reach {expected}: "
            f"{container.runtime_v2_repository.get_run(run_id).status}"
        )

    async def test_read_only_command_waits_for_approval_by_default(self) -> None:
        provider = ScriptedShellProvider("ls")
        client, app = await self._app(provider, env={"ENDLESS_TASK_SHELL_TRUST_READONLY": "0"})

        _conversation_id, run_id = await self._submit(client, "k-off")

        await self._wait_status(
            app.state.container, run_id, RunStatus.WAITING_APPROVAL
        )

    async def test_read_only_command_auto_approved_when_trust_enabled(self) -> None:
        provider = ScriptedShellProvider("ls")
        client, app = await self._app(provider, env={"ENDLESS_TASK_SHELL_TRUST_READONLY": "1"})

        _conversation_id, run_id = await self._submit(client, "k-on")

        status = await wait_for_run_terminal(app.state.container, run_id)
        self.assertEqual(RunStatus.COMPLETED, status)

    async def test_write_command_still_waits_when_trust_enabled(self) -> None:
        provider = ScriptedShellProvider("printf x > out.txt")
        client, app = await self._app(provider, env={"ENDLESS_TASK_SHELL_TRUST_READONLY": "1"})

        _conversation_id, run_id = await self._submit(client, "k-write")

        await self._wait_status(
            app.state.container, run_id, RunStatus.WAITING_APPROVAL
        )


if __name__ == "__main__":
    unittest.main()
