"""S5 模型侧终端工具：open/send/read/signal/close/list（真实 PTY）。"""

from __future__ import annotations

import asyncio
import os
import pty
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolApprovalMode, ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.terminal import TerminalService
from endless_task.workspace_runtime.terminal_tools import build_terminal_tools


def _pty_available() -> bool:
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return False
    os.close(master_fd)
    os.close(slave_fd)
    return True


PTY_AVAILABLE = _pty_available()


def _call(conversation_id: str, **arguments) -> ToolCall:
    return ToolCall(
        id="call_terminal",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="tool",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-09T00:00:00.000Z",
    )


class TerminalToolDefinitionTest(unittest.TestCase):
    def test_approval_modes_and_confirmation(self) -> None:
        tools = {tool.definition.name: tool for tool in build_terminal_tools(None, None)}

        for name in ("terminal_open", "terminal_send", "terminal_signal"):
            self.assertEqual(
                ToolApprovalMode.REQUIRED, tools[name].definition.approval_mode, name
            )
            self.assertTrue(
                tools[name].requires_explicit_confirmation(_call("c", text="ls")), name
            )
        for name in ("terminal_read", "terminal_list", "terminal_close"):
            self.assertEqual(
                ToolApprovalMode.AUTO, tools[name].definition.approval_mode, name
            )
            self.assertFalse(tools[name].requires_explicit_confirmation(_call("c")))

    def test_send_prompt_marks_dangerous_commands(self) -> None:
        tool = {item.definition.name: item for item in build_terminal_tools(None, None)}[
            "terminal_send"
        ]
        prompt = tool.approval_prompt(
            _call("c", session_id="term_1", text="rm -rf build")
        )
        self.assertIn("危险命令", prompt.summary)
        self.assertIn("rm -rf build", prompt.reason)


@unittest.skipUnless(PTY_AVAILABLE, "host cannot allocate a PTY (/dev/ptmx denied)")
class TerminalToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "tools.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(self.workspace.id, str(self.root))
        self.conversation = self.chat.create_or_reuse_empty_conversation(
            self.workspace.id
        )
        self.service = TerminalService()
        self.addAsyncCleanup(self.service.close_all)
        self.tools = {
            tool.definition.name: tool
            for tool in build_terminal_tools(self.resolver, self.service)
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _open(self, **arguments):
        return await self.tools["terminal_open"].execute(
            _call(self.conversation.id, **arguments), CancellationToken()
        )

    async def _send(self, session_id: str, text: str, **extra):
        return await self.tools["terminal_send"].execute(
            _call(self.conversation.id, session_id=session_id, text=text, **extra),
            CancellationToken(),
        )

    async def test_open_send_read_signal_close_flow(self) -> None:
        opened = await self._open(name="model")
        session_id = opened.structured_content["terminal"]["sessionId"]
        self.assertEqual("running", opened.structured_content["terminal"]["status"]["kind"])

        sent = await self._send(
            session_id, "for f in model-alpha model-beta; do echo got-$f; done"
        )
        self.assertEqual("stdin_read", sent.structured_content["waitReason"])
        self.assertIn("got-model-beta", sent.structured_content["viewport"])
        self.assertIn("terminal", sent.content)

        read = await self.tools["terminal_read"].execute(
            _call(self.conversation.id, session_id=session_id, count=50),
            CancellationToken(),
        )
        self.assertIn("got-model-alpha", read.structured_content["viewport"])

        listed = await self.tools["terminal_list"].execute(
            _call(self.conversation.id), CancellationToken()
        )
        self.assertEqual(1, len(listed.structured_content["items"]))

        await self._send(session_id, "sleep 30", timeout_seconds=1)
        interrupted = await self.tools["terminal_signal"].execute(
            _call(self.conversation.id, session_id=session_id, signal="SIGINT"),
            CancellationToken(),
        )
        self.assertEqual("SIGINT", interrupted.structured_content["signal"])

        closed = await self.tools["terminal_close"].execute(
            _call(self.conversation.id, session_id=session_id), CancellationToken()
        )
        self.assertTrue(closed.structured_content["closed"])
        self.assertEqual((), self.service.list_for_workspace(self.workspace.id))

    async def test_long_running_command_reports_inferred_idle(self) -> None:
        opened = await self._open()
        session_id = opened.structured_content["terminal"]["sessionId"]

        sent = await self._send(session_id, "sleep 30", timeout_seconds=3)

        self.assertEqual("inferred_idle", sent.structured_content["waitReason"])
        self.assertIn("不代表前台命令已退出", sent.content)

    async def test_exit_is_reported_as_session_exit(self) -> None:
        opened = await self._open()
        session_id = opened.structured_content["terminal"]["sessionId"]

        sent = await self._send(session_id, "exit 3", timeout_seconds=5)

        self.assertEqual("session_exit", sent.structured_content["waitReason"])
        self.assertEqual("exited", sent.structured_content["sessionStatus"]["kind"])

    async def test_unknown_session_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            await self._send("term_missing", "echo hi")
        self.assertEqual("terminal_session_not_found", ctx.exception.code)

    async def test_send_on_exited_session_is_rejected(self) -> None:
        opened = await self._open()
        session_id = opened.structured_content["terminal"]["sessionId"]
        await self._send(session_id, "exit 0", timeout_seconds=5)

        with self.assertRaises(ToolError) as ctx:
            await self._send(session_id, "echo hi")

        self.assertEqual("terminal_session_exited", ctx.exception.code)

    async def test_viewport_is_bounded(self) -> None:
        opened = await self._open()
        session_id = opened.structured_content["terminal"]["sessionId"]

        sent = await self._send(
            session_id,
            "for i in {1..400}; do echo padding-padding-padding-padding-padding-$i; done",
            timeout_seconds=10,
        )

        self.assertTrue(sent.structured_content["truncated"])
        self.assertLessEqual(
            len(sent.structured_content["viewport"].encode("utf-8")), 16 * 1024
        )

    async def test_signal_failure_on_exited_session(self) -> None:
        opened = await self._open()
        session_id = opened.structured_content["terminal"]["sessionId"]
        await self._send(session_id, "exit 0", timeout_seconds=5)
        await asyncio.sleep(0.2)

        with self.assertRaises(ToolError) as ctx:
            await self.tools["terminal_signal"].execute(
                _call(self.conversation.id, session_id=session_id, signal="SIGINT"),
                CancellationToken(),
            )

        self.assertEqual("terminal_signal_failed", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()


class TerminalToolsFlagTest(unittest.TestCase):
    """S5 工具默认不注册；ENDLESS_TASK_TERMINAL_TOOLS=1 才注册 6 个。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _registered(self, flag: str) -> set[str]:
        from unittest import mock

        from endless_task.api import AppSettings, create_app
        from endless_task.runtime import FakeProvider

        env = {"ENDLESS_TASK_TERMINAL_TOOLS": flag}
        with mock.patch.dict(os.environ, env, clear=False):
            app = create_app(
                settings=AppSettings(
                    database_path=Path(self._tmp.name) / f"flag-{flag}.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
        return {
            definition.name
            for definition in app.state.container.tool_registry.definitions()
            if definition.name.startswith("terminal_")
        }

    def test_disabled_by_default(self) -> None:
        self.assertEqual(set(), self._registered("0"))

    def test_enabled_registers_six_tools(self) -> None:
        self.assertEqual(
            {
                "terminal_open",
                "terminal_send",
                "terminal_read",
                "terminal_signal",
                "terminal_close",
                "terminal_list",
            },
            self._registered("1"),
        )
