"""M1：MCP ``tools/list_changed`` 热同步与两阶段代际切换。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from endless_task.mcp_runtime.manager import McpManager
from endless_task.storage import Database
from endless_task.storage.sqlite_mcp_server_repository import (
    McpServerDraft,
    SqliteMcpServerRepository,
)
from endless_task.tooling import ToolRegistry


class FakeSession:
    """按脚本返回工具列表；可注入异常模拟拉取失败。"""

    def __init__(self, tools: list[list[Any]]) -> None:
        self._script = list(tools)
        self.calls = 0

    async def list_tools(self, cursor: str | None = None):
        del cursor
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        result = self._script[index]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(tools=list(result), nextCursor=None)


def _tool(name: str) -> Any:
    return SimpleNamespace(
        name=name,
        description=f"tool {name}",
        inputSchema={"type": "object", "properties": {}},
        annotations=None,
    )


class McpHotSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "hot.db")
        self.database.initialize()
        self.repository = SqliteMcpServerRepository(self.database)
        self.config = self.repository.create_server(
            McpServerDraft(name="demo", transport="stdio", command="python")
        )
        self.registry = ToolRegistry()
        self.manager = McpManager(
            repository=self.repository, tool_registry=self.registry
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _connection(self, session: FakeSession):
        from endless_task.mcp_runtime.manager import _LiveConnection

        connection = _LiveConnection(
            config=self.config,
            session=session,
            stop_event=asyncio.Event(),
            failure_event=asyncio.Event(),
            task=None,
            tools=(),
            state="connected",
        )
        self.manager._connections[self.config.id] = connection
        return connection

    def _names(self) -> list[str]:
        return sorted(
            definition.name for definition in self.registry.definitions()
        )

    async def test_sync_adds_and_removes_tools(self) -> None:
        session = FakeSession([[_tool("echo")], [_tool("echo"), _tool("sum")]])
        connection = self._connection(session)

        self.assertTrue(await self.manager.sync_tools(self.config, connection))
        self.assertEqual(["mcp__demo__echo"], self._names())

        self.assertTrue(await self.manager.sync_tools(self.config, connection))
        self.assertEqual(
            ["mcp__demo__echo", "mcp__demo__sum"], self._names()
        )
        self.assertEqual(2, connection.sync_generation)
        self.assertEqual("connected", connection.state)

        # 服务器删除工具 → 同步后旧工具消失
        session._script.append([_tool("echo")])
        self.assertTrue(await self.manager.sync_tools(self.config, connection))
        self.assertEqual(["mcp__demo__echo"], self._names())

    async def test_fetch_failure_keeps_old_generation(self) -> None:
        session = FakeSession([[_tool("echo")], RuntimeError("boom")])
        connection = self._connection(session)
        await self.manager.sync_tools(self.config, connection)
        self.assertEqual(["mcp__demo__echo"], self._names())

        self.assertFalse(await self.manager.sync_tools(self.config, connection))
        # 旧一代仍在，且错误被记录
        self.assertEqual(["mcp__demo__echo"], self._names())
        self.assertEqual(1, connection.sync_generation)
        self.assertIn("刷新失败", self.manager.status(self.config.id).last_error or "")

    async def test_registration_conflict_rolls_back(self) -> None:
        session = FakeSession([[_tool("echo")]])
        connection = self._connection(session)
        await self.manager.sync_tools(self.config, connection)

        # 制造跨服务器名称冲突：另一个连接也提供 mcp__demo__echo
        from endless_task.mcp_runtime.manager import _LiveConnection

        other_config = self.repository.create_server(
            McpServerDraft(name="demo2", transport="stdio", command="python")
        )
        other_session = FakeSession([[_tool("echo")]])
        other = _LiveConnection(
            config=other_config,
            session=other_session,
            stop_event=asyncio.Event(),
            failure_event=asyncio.Event(),
            task=None,
            tools=(),
            state="connected",
        )
        self.manager._connections[other_config.id] = other

        # 新代际里 demo2 抢占了 demo 的公开名（模拟归一化后冲突）
        from endless_task.mcp_runtime.tool import McpToolBridge

        colliding = McpToolBridge(
            server_name="demo",
            raw_name="echo",
            description="x",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            destructive=False,
            timeout_seconds=5.0,
            session_provider=lambda _: None,
        )
        connection.tools = (colliding,)
        other.tools = (colliding,)
        with self.assertRaises(Exception):
            self.manager._registry_replace_with({})
        # 校验失败不修改连接状态
        self.assertEqual((colliding,), connection.tools)

    async def test_concurrent_notifications_are_coalesced(self) -> None:
        session = FakeSession([[_tool("echo")], [_tool("echo"), _tool("sum")]])
        connection = self._connection(session)

        first, second = await asyncio.gather(
            self.manager.sync_tools(self.config, connection),
            self.manager.sync_tools(self.config, connection),
        )
        self.assertTrue(first or second)
        # 合并后只应消费两轮（首轮 + pending 合并轮），不会并发注册
        self.assertLessEqual(connection.sync_generation, 2)
        self.assertEqual(
            ["mcp__demo__echo", "mcp__demo__sum"], self._names()
        )

    async def test_notification_triggers_sync(self) -> None:
        from mcp import types as mcp_types

        session = FakeSession([[_tool("echo")], [_tool("echo"), _tool("sum")]])
        connection = self._connection(session)
        await self.manager.sync_tools(self.config, connection)
        self.assertEqual(["mcp__demo__echo"], self._names())

        notification = mcp_types.ToolListChangedNotification(
            method="notifications/tools/list_changed"
        )
        await self.manager.handle_notification(
            SimpleNamespace(root=notification), self.config, session
        )
        # 通知处理只负责投递后台任务（避免 SDK 消息循环死锁），这里等它跑完。
        self.assertIsNotNone(connection.sync_task)
        await connection.sync_task
        self.assertEqual(["mcp__demo__echo", "mcp__demo__sum"], self._names())


if __name__ == "__main__":
    unittest.main()
