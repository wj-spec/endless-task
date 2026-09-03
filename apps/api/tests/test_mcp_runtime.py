from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.mcp_runtime import McpToolBridge, public_tool_name
from endless_task.mcp_runtime.manager import McpManager
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import Database
from endless_task.storage.sqlite_mcp_server_repository import (
    McpServerConfig,
    McpServerDraft,
    SqliteMcpServerRepository,
)
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
    ToolRegistry,
)


def _tool_call(**arguments: Any) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="mcp__demo__echo",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-08-28T00:00:00.000Z",
    )


class PublicToolNameTest(unittest.TestCase):
    def test_clean_name_is_stable(self) -> None:
        self.assertEqual(
            "mcp__github__create_issue",
            public_tool_name("github", "create_issue"),
        )

    def test_invalid_characters_are_normalized_without_collision(self) -> None:
        first = public_tool_name("web", "search.code")
        second = public_tool_name("web", "search_code")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("mcp__web__search_code_"))

    def test_long_name_is_bounded_and_deterministic(self) -> None:
        raw_name = "x" * 100
        first = public_tool_name("server", raw_name)
        second = public_tool_name("server", raw_name)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 64)


class McpServerRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "mcp.db")
        self.database.initialize()
        self.repository = SqliteMcpServerRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_create_list_update_and_delete(self) -> None:
        created = self.repository.create_server(
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="python",
                args=("-m", "demo"),
                env={"DEMO": "1"},
            )
        )
        self.assertEqual("demo", created.name)
        self.assertEqual(("python",), (created.command,))
        self.assertEqual(1, len(self.repository.list_servers()))

        updated = self.repository.update_server(
            created.id,
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="node",
                args=("server.js",),
                enabled=False,
            ),
        )
        self.assertEqual("node", updated.command)
        self.assertFalse(updated.enabled)

        self.repository.delete_server(created.id)
        self.assertEqual((), self.repository.list_servers())

    def test_duplicate_name_is_rejected(self) -> None:
        self.repository.create_server(
            McpServerDraft(name="demo", transport="stdio", command="node")
        )
        with self.assertRaises(Exception):
            self.repository.create_server(
                McpServerDraft(name="demo", transport="stdio", command="node")
            )

    def test_validation_rejects_invalid_name_and_missing_transport_fields(self) -> None:
        with self.assertRaises(Exception):
            self.repository.create_server(
                McpServerDraft(name="Demo", transport="stdio", command="node")
            )
        with self.assertRaises(Exception):
            self.repository.create_server(
                McpServerDraft(name="demo", transport="stdio", command="")
            )
        with self.assertRaises(Exception):
            self.repository.create_server(
                McpServerDraft(name="demo", transport="http", command="", url="ftp://x")
            )


_DEFAULT_STRUCTURED_CONTENT = object()


class FakeMcpSession:
    def __init__(self, structured_content: Any = _DEFAULT_STRUCTURED_CONTENT) -> None:
        self.structured_content = (
            {"ok": True}
            if structured_content is _DEFAULT_STRUCTURED_CONTENT
            else structured_content
        )

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any):
        del arguments
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"hello {name}")],
            structuredContent=self.structured_content,
            isError=False,
        )


class McpToolBridgeTest(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, **overrides: Any) -> McpToolBridge:
        options = {
            "server_name": "demo",
            "raw_name": "echo",
            "description": "Echo text",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "read_only": True,
            "destructive": False,
            "timeout_seconds": 5.0,
            "session_provider": lambda server_name: FakeMcpSession(),
        }
        options.update(overrides)
        return McpToolBridge(**options)

    def test_definition_uses_read_only_policy(self) -> None:
        bridge = self._bridge()
        self.assertEqual("mcp__demo__echo", bridge.definition.name)
        self.assertEqual(ToolEffect.READ_ONLY, bridge.definition.effect)
        self.assertEqual(ToolApprovalMode.AUTO, bridge.definition.approval_mode)

    def test_definition_accepts_standard_mcp_schema_features(self) -> None:
        bridge = self._bridge(
            input_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": {"text": {"type": "string", "minLength": 1}},
                "type": "object",
                "properties": {
                    "text": {"$ref": "#/$defs/text"},
                    "format": {
                        "anyOf": [
                            {"const": "plain"},
                            {"const": "markdown"},
                        ]
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            }
        )

        self.assertIn("$defs", bridge.definition.input_schema)
        self.assertIn(
            "anyOf",
            bridge.definition.input_schema["properties"]["format"],
        )

    def test_non_read_only_tools_always_require_confirmation(self) -> None:
        for destructive in (False, True):
            with self.subTest(destructive=destructive):
                bridge = self._bridge(
                    read_only=False,
                    destructive=destructive,
                )
                self.assertEqual(
                    ToolEffect.EXTERNAL_ACTION,
                    bridge.definition.effect,
                )
                self.assertEqual(
                    ToolApprovalMode.REQUIRED,
                    bridge.definition.approval_mode,
                )
                self.assertTrue(
                    bridge.requires_explicit_confirmation(_tool_call(text="x"))
                )

    async def test_execute_projects_text_and_structured_content(self) -> None:
        bridge = self._bridge()
        result = await bridge.execute(_tool_call(text="x"), CancellationToken())
        self.assertIn("hello echo", result.content)
        self.assertEqual({"ok": True}, dict(result.structured_content or {}))
    async def test_execute_preserves_every_json_structured_content_type(self) -> None:
        values = (
            {"ok": True},
            ["first", 2],
            "plain text",
            42,
            3.5,
            False,
            None,
        )

        for value in values:
            with self.subTest(value=value):
                bridge = self._bridge(
                    session_provider=lambda server_name, value=value: FakeMcpSession(
                        value
                    )
                )
                result = await bridge.execute(
                    _tool_call(text="x"),
                    CancellationToken(),
                )
                self.assertEqual(value, result.structured_content)


class DynamicToolRegistryTest(unittest.TestCase):
    def test_replace_tools_updates_namespace_atomically(self) -> None:
        registry = ToolRegistry()
        manager = object()

        def bridge(name: str) -> McpToolBridge:
            return McpToolBridge(
                server_name="demo",
                raw_name=name,
                description=f"tool {name}",
                input_schema={"type": "object", "properties": {}},
                read_only=True,
                destructive=False,
                timeout_seconds=5.0,
                session_provider=lambda server_name: None,
            )

        del manager
        first = bridge("first")
        second = bridge("second")
        registry.replace_tools((first, second))
        self.assertEqual(
            ("mcp__demo__first", "mcp__demo__second"),
            tuple(item.name for item in registry.definitions()),
        )
        registry.replace_tools((first,))
        self.assertEqual(
            ("mcp__demo__first",),
            tuple(
                item.name
                for item in registry.definitions()
                if item.name.startswith("mcp__")
            ),
        )


class McpDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_converts_annotations_and_schema(self) -> None:
        database = Database(Path(tempfile.mkdtemp()) / "mcp.db")
        database.initialize()
        repository = SqliteMcpServerRepository(database)
        config = McpServerConfig(
            id="mcp_1",
            name="demo",
            transport="stdio",
            command="node",
            args=(),
            env={},
            cwd="",
            url="",
            headers={},
            enabled=True,
            tool_call_timeout_seconds=5.0,
            created_at="2026-08-28T00:00:00.000Z",
            updated_at="2026-08-28T00:00:00.000Z",
        )
        manager = McpManager(repository=repository, tool_registry=ToolRegistry())

        class ListResult:
            nextCursor = None
            tools = [
                SimpleNamespace(
                    name="echo",
                    description="Echo",
                    inputSchema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    annotations=SimpleNamespace(
                        readOnlyHint=True, destructiveHint=False
                    ),
                ),
                SimpleNamespace(
                    name="remove",
                    description="Remove",
                    inputSchema={"type": "object", "properties": {}},
                    annotations=SimpleNamespace(
                        readOnlyHint=False, destructiveHint=True
                    ),
                ),
            ]

        class Session:
            async def list_tools(self, cursor=None):
                del cursor
                return ListResult()

        tools = await manager._discover_tools(config, Session())
        self.assertEqual(("mcp__demo__echo", "mcp__demo__remove"), tuple(item.public_name for item in tools))
        self.assertEqual(ToolEffect.READ_ONLY, tools[0].definition.effect)
        self.assertEqual(ToolEffect.EXTERNAL_ACTION, tools[1].definition.effect)


class McpStdioE2ETest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_connection_discovery_and_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "mcp.db")
            database.initialize()
            repository = SqliteMcpServerRepository(database)
            server = repository.create_server(
                McpServerDraft(
                    name="demo",
                    transport="stdio",
                    command=sys.executable,
                    args=(str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py"),),
                )
            )
            registry = ToolRegistry()
            effect_log = EffectLog(Path(directory) / "logs")
            manager = McpManager(
                repository=repository,
                tool_registry=registry,
                effect_log=effect_log,
            )
            try:
                await manager.start_all()
                status = manager.status(server.id)
                self.assertEqual("connected", status.state)
                self.assertEqual(1, status.tool_count)
                self.assertEqual(
                    "mcp__demo__echo", registry.definitions()[-1].name
                )
                tool = registry.resolve("mcp__demo__echo")
                result = await tool.execute(
                    _tool_call(text="hello mcp"), CancellationToken()
                )
                self.assertIn("hello mcp", result.content)
                log_files = list((Path(directory) / "logs").glob("effects_*.jsonl"))
                self.assertEqual(1, len(log_files))
                self.assertIn("mcp__demo__echo", log_files[0].read_text(encoding="utf-8"))
            finally:
                await manager.stop_all()


class McpReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_state(
        self, manager: McpManager, server_id: str, state: str, timeout: float = 2.0
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if manager.status(server_id).state == state:
                return
            await asyncio.sleep(0.01)
        self.assertEqual(state, manager.status(server_id).state)

    async def test_crash_reconnects_and_keeps_tool_registered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "mcp.db")
            database.initialize()
            repository = SqliteMcpServerRepository(database)
            server = repository.create_server(
                McpServerDraft(
                    name="demo",
                    transport="stdio",
                    command=sys.executable,
                    args=(
                        str(
                            Path(__file__).parent
                            / "fixtures"
                            / "crash_mcp_server.py"
                        ),
                    ),
                )
            )
            registry = ToolRegistry()
            manager = McpManager(
                repository=repository,
                tool_registry=registry,
                reconnect_initial_delay_seconds=0.01,
                reconnect_max_delay_seconds=0.02,
            )
            try:
                await manager.start_all()
                self.assertEqual("connected", manager.status(server.id).state)
                tools_before_failure = tuple(
                    item.name for item in registry.definitions()
                )

                with self.assertRaises(Exception):
                    await registry.resolve("mcp__demo__crash").execute(
                        _tool_call(), CancellationToken()
                    )

                deadline = asyncio.get_running_loop().time() + 2
                saw_reconnecting = False
                tools_stable_during_reconnect = False
                while asyncio.get_running_loop().time() < deadline:
                    state = manager.status(server.id).state
                    if state == "reconnecting":
                        saw_reconnecting = True
                        tools_stable_during_reconnect = tools_stable_during_reconnect or (
                            tuple(item.name for item in registry.definitions())
                            == tools_before_failure
                        )
                    if manager.status(server.id).state == "connected":
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(saw_reconnecting)
                self.assertTrue(tools_stable_during_reconnect)
                self.assertEqual("connected", manager.status(server.id).state)
                self.assertEqual(
                    tools_before_failure,
                    tuple(item.name for item in registry.definitions()),
                )
                result = await registry.resolve("mcp__demo__echo").execute(
                    _tool_call(text="recovered"), CancellationToken()
                )
                self.assertIn("recovered", result.content)
            finally:
                await manager.stop_all()

    async def test_health_check_detects_idle_connection_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "mcp.db")
            database.initialize()
            repository = SqliteMcpServerRepository(database)
            server = repository.create_server(
                McpServerDraft(
                    name="demo",
                    transport="stdio",
                    command=sys.executable,
                    args=(
                        str(
                            Path(__file__).parent
                            / "fixtures"
                            / "short_lived_mcp_server.py"
                        ),
                    ),
                )
            )
            registry = ToolRegistry()
            manager = McpManager(
                repository=repository,
                tool_registry=registry,
                # 使用较大的重连退避，使「reconnecting」状态可被稳定观察到，
                # 避免在极短窗口内被轮询错过造成偶发 flake。
                reconnect_initial_delay_seconds=0.5,
                reconnect_max_delay_seconds=1.0,
                reconnect_max_attempts=4,
                health_check_seconds=0.01,
            )
            try:
                await manager.start_all()
                self.assertEqual("connected", manager.status(server.id).state)
                # 断言「空闲断连被健康检查检测」：子进程到点退出，manager 应进入
                # reconnecting 态。用较大的重连退避（0.5/1.0s）让该瞬态稳定可观察，
                # 避免被轮询错过。给足预算（10s）应对机器负载下的子进程拉起。
                deadline = asyncio.get_running_loop().time() + 10
                saw_reconnecting = False
                while asyncio.get_running_loop().time() < deadline:
                    state = manager.status(server.id).state
                    saw_reconnecting = saw_reconnecting or state == "reconnecting"
                    if saw_reconnecting and state == "connected":
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(saw_reconnecting)
                self.assertEqual("connected", manager.status(server.id).state)
            finally:
                await manager.stop_all()

    async def test_reconnect_budget_exhausts_and_unregisters_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            database = Database(Path(directory) / "mcp.db")
            database.initialize()
            repository = SqliteMcpServerRepository(database)
            server = repository.create_server(
                McpServerDraft(
                    name="demo",
                    transport="stdio",
                    command=sys.executable,
                    args=(
                        str(
                            Path(__file__).parent
                            / "fixtures"
                            / "flaky_mcp_server.py"
                        ),
                        str(marker),
                    ),
                )
            )
            registry = ToolRegistry()
            manager = McpManager(
                repository=repository,
                tool_registry=registry,
                reconnect_initial_delay_seconds=0.05,
                # reconnect budget 的语义是「reconnect_max_delay_seconds 窗口内的
                # 快速失败连击」：任何一次尝试的耗时达到窗口值都会把 attempts 归零
                # 重新起算。因此窗口必须显著大于真实子进程拉起耗时（python 启动 +
                # marker 判定约 20-60ms）。旧配置 0.02s 低于任何真实尝试耗时，预算
                # 结构性不可达，manager 永远停在 reconnecting，导致本用例 flake。
                reconnect_max_delay_seconds=1.0,
                reconnect_max_attempts=2,
            )
            try:
                await manager.start_all()
                self.assertEqual("connected", manager.status(server.id).state)
                with self.assertRaises(Exception):
                    await registry.resolve("mcp__demo__crash").execute(
                        _tool_call(), CancellationToken()
                    )
                await self._wait_for_state(manager, server.id, "error")
                self.assertEqual((), tuple(
                    item.name
                    for item in registry.definitions()
                    if item.name.startswith("mcp__")
                ))
                self.assertIn("连续重连失败", manager.status(server.id).last_error or "")
            finally:
                await manager.stop_all()


class McpApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_server_crud_without_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "args": ["server.js"],
                            "enabled": False,
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    server = created.json()["server"]
                    self.assertEqual("disabled", server["state"])

                    listed = await client.get("/mcp/servers")
                    self.assertEqual(1, len(listed.json()["items"]))

                    patched = await client.patch(
                        f"/mcp/servers/{server['id']}",
                        json={"enabled": False},
                    )
                    self.assertEqual(200, patched.status_code)

                    deleted = await client.delete(f"/mcp/servers/{server['id']}")
                    self.assertEqual(204, deleted.status_code)
                    self.assertEqual(
                        0,
                        len((await client.get("/mcp/servers")).json()["items"]),
                    )
            finally:
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
