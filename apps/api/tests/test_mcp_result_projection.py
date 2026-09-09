"""M2：MCP 结果投影（图片落盘 / 资源链接 / 诊断文本）与调用观测。"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.mcp_runtime import McpToolBridge
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import Database
from endless_task.storage.sqlite_mcp_server_repository import (
    McpServerDraft,
    SqliteMcpServerRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus
from endless_task.workspace_runtime.effect_log import EffectLog


def _call() -> ToolCall:
    return ToolCall(
        id="call_img",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="mcp__demo__shot",
        arguments={},
        status=ToolCallStatus.CREATED,
        created_at="2026-09-09T00:00:00.000Z",
    )


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Session:
    def __init__(self, content: list[Any], *, is_error: bool = False) -> None:
        self._content = content
        self._is_error = is_error

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any):
        del name, arguments
        return SimpleNamespace(
            content=self._content,
            structuredContent=None,
            isError=self._is_error,
        )


class McpResultProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        self.effect_log = EffectLog(Path(self._tmp.name) / "logs")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _bridge(self, session: _Session, *, with_root: bool = True) -> McpToolBridge:
        return McpToolBridge(
            server_name="demo",
            raw_name="shot",
            description="screenshot",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            destructive=False,
            timeout_seconds=5.0,
            session_provider=lambda _: session,
            effect_log=self.effect_log,
            workspace_root_provider=(
                (lambda _conversation_id: self.root) if with_root else None
            ),
        )

    async def test_image_is_stored_and_referenced(self) -> None:
        session = _Session(
            [
                SimpleNamespace(type="text", text="看这张图"),
                SimpleNamespace(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(PNG_BYTES).decode("ascii"),
                ),
                SimpleNamespace(
                    type="resource_link",
                    name="报告",
                    uri="https://example.com/report",
                ),
            ]
        )
        result = await self._bridge(session).execute(_call(), CancellationToken())

        self.assertIn("看这张图", result.content)
        self.assertIn("图片已保存到工作区", result.content)
        self.assertIn("资源链接: 报告 https://example.com/report", result.content)
        attachments = result.structured_content["attachments"]
        self.assertEqual(1, len(attachments))
        stored = self.root / attachments[0]["path"]
        self.assertTrue(stored.is_file())
        self.assertEqual(PNG_BYTES, stored.read_bytes())
        self.assertTrue(
            attachments[0]["path"].startswith(".endless-task/mcp-attachments/demo/")
        )

    async def test_unsupported_image_type_is_diagnostic_text(self) -> None:
        session = _Session(
            [
                SimpleNamespace(
                    type="image",
                    mimeType="image/tiff",
                    data=base64.b64encode(b"x").decode("ascii"),
                ),
                SimpleNamespace(type="audio", data="zzz"),
            ]
        )
        result = await self._bridge(session).execute(_call(), CancellationToken())
        self.assertIn("图片内容无法保存", result.content)
        self.assertIn("未支持的 MCP 内容类型: audio", result.content)

    async def test_call_is_logged_with_duration_and_status(self) -> None:
        session = _Session([SimpleNamespace(type="text", text="ok")])
        await self._bridge(session).execute(_call(), CancellationToken())
        entries = self.effect_log.list_for_operation("mcp__demo__")
        self.assertEqual(1, len(entries))
        self.assertIn("status=ok", entries[0]["detail"])
        self.assertIn("resultChars=", entries[0]["detail"])
        self.assertIn("durationMs", entries[0])

    async def test_error_call_is_logged_as_error(self) -> None:
        from endless_task.tooling import ToolError

        session = _Session(
            [SimpleNamespace(type="text", text="boom")], is_error=True
        )
        with self.assertRaises(ToolError):
            await self._bridge(session).execute(_call(), CancellationToken())
        entries = self.effect_log.list_for_operation("mcp__demo__")
        self.assertEqual(1, len(entries))
        self.assertIn("status=error", entries[0]["detail"])


class McpCallsApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_calls_endpoint_returns_recent_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True)
            app = create_app(
                settings=AppSettings(
                    database_path=data_dir / "app.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            try:
                created = await client.post(
                    "/mcp/servers",
                    json={
                        "name": "demo",
                        "transport": "stdio",
                        "command": "python",
                    },
                )
                self.assertEqual(201, created.status_code, created.text)
                server_id = created.json()["server"]["id"]

                container = app.state.container
                container.effect_log.append(
                    conversation_id="conv_1",
                    workspace_id=None,
                    workspace_root="MCP",
                    operation="mcp__demo__echo",
                    detail="server=demo; rawTool=echo; status=ok; resultChars=2",
                    receipt=_receipt(),
                    duration_ms=12,
                )
                response = await client.get(f"/mcp/servers/{server_id}/calls")
                self.assertEqual(200, response.status_code, response.text)
                items = response.json()["items"]
                self.assertEqual(1, len(items))
                self.assertEqual("mcp__demo__echo", items[0]["operation"])
                self.assertEqual("ok", items[0]["status"])
                self.assertEqual(12, items[0]["durationMs"])
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)


def _receipt():
    from endless_task.workspace_runtime.effect_log import EffectReceipt

    return EffectReceipt(
        kind="mcp_call", path="mcp__demo__echo", executed_at="2026-09-09T00:00:00Z"
    )


if __name__ == "__main__":
    unittest.main()


class McpEvalClassificationTest(unittest.TestCase):
    """M2：MCP 工具是动态名称，不应被 approval_gate 当成"未知效果"。"""

    def test_mcp_prefix_is_not_unknown_effect(self) -> None:
        from endless_task.eval.evaluators import ApprovalGateEvaluator
        from endless_task.runtime_v2 import ToolExecutionStatus

        record = SimpleNamespace(
            tool_name="mcp__img__shot",
            status=ToolExecutionStatus.COMPLETED,
            approval_id=None,
            derived_status=ToolExecutionStatus.COMPLETED,
            arguments={},
        )
        context = SimpleNamespace(
            replay=SimpleNamespace(
                model_turns=[
                    SimpleNamespace(
                        tool_executions=[SimpleNamespace(record=record)]
                    )
                ]
            ),
            write_tools=frozenset(),
            read_only_tools=frozenset(),
            auto_write_tools=frozenset(),
        )
        metrics = list(ApprovalGateEvaluator().evaluate(context))
        unknown = next(
            metric
            for metric in metrics
            if metric.key == "ungated_unknown_tool_count"
        )
        self.assertEqual(0, unknown.value)
