"""S5 审批证据：审批通过后 execution 记录带 approval_id（供 eval approval_gate 判定）。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

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
    name = "scripted-shell-approval"

    def __init__(self, command: str) -> None:
        self._command = command
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_shell_approval",
                name="run_shell",
                arguments={"command": self._command},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(finish_reason="stop")


class ApprovalEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_approved_tool_execution_records_approval_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            provider = ScriptedShellProvider("printf approval-evidence")
            app = create_app(
                settings=AppSettings(
                    database_path=Path(tmp) / "approval.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                ),
                provider=SwappableProvider(provider),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            try:
                created = await client.post(
                    "/workspaces",
                    json={"name": "审批证据", "rootPath": str(root)},
                )
                workspace_id = created.json()["workspace"]["id"]
                conversation = await client.post(
                    "/conversations", json={"workspaceId": workspace_id}
                )
                conversation_id = conversation.json()["id"]
                handle = await send_message(
                    client, conversation_id, "开始", idempotency_key="k-approval"
                )
                run_id = handle["runId"]

                approval_id = await self._approve_pending(app, conversation_id)
                self.assertIsNotNone(approval_id)

                status = await wait_for_run_terminal(app.state.container, run_id)
                self.assertEqual(RunStatus.COMPLETED, status)

                repository = app.state.container.runtime_v2_repository
                records = [
                    record
                    for turn in repository.list_model_turns(run_id)
                    for record in repository.list_tool_executions(turn.id)
                    if record.tool_name == "run_shell"
                ]
                self.assertEqual(1, len(records))
                self.assertEqual(approval_id, records[0].approval_id)
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)

    async def _approve_pending(self, app, conversation_id: str) -> str | None:
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            pending = app.state.container.runtime_v2_gateway.snapshot(
                conversation_id
            ).get("pendingApprovals") or []
            if pending:
                approval_id = str(pending[0]["id"])
                from endless_task.runtime_v2 import ToolApprovalDecision

                await app.state.container.runtime_v2_gateway.resolve_approval(
                    approval_id, ToolApprovalDecision.APPROVE
                )
                return approval_id
            await asyncio.sleep(0.02)
        return None


if __name__ == "__main__":
    unittest.main()
