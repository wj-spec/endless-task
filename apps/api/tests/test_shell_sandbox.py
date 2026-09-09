"""S9 终端沙箱：run_shell 走 ExecutionEnvironment 后端（fail closed）。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from endless_task.agent_platform import AgentPlatformError
from endless_task.execution_env import (
    FakeExecutionEnvironment,
    NetworkMode,
    ProcessResult,
)
from endless_task.execution_env.seatbelt import sandbox_apply_failed
from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.shell_tool import RunShellTool


def _call(conversation_id: str, command: str) -> ToolCall:
    return ToolCall(
        id="call_sandbox",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="run_shell",
        arguments={"command": command},
        status=ToolCallStatus.CREATED,
        created_at="2026-09-09T00:00:00.000Z",
    )


class SandboxApplyFailedTest(unittest.TestCase):
    def test_only_sandbox_exec_own_failures_are_detected(self) -> None:
        self.assertTrue(
            sandbox_apply_failed(b"sandbox-exec: sandbox_apply: Operation not permitted")
        )
        self.assertTrue(sandbox_apply_failed(b"sandbox-exec: unbound variable: x"))
        self.assertFalse(sandbox_apply_failed(b"ls: no such file or directory"))
        self.assertFalse(sandbox_apply_failed(b""))


class ShellSandboxToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "sandbox.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.root = Path(self._temp.name) / "ws"
        self.root.mkdir()
        self.workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(self.workspace.id, str(self.root))
        self.conversation = self.chat.create_or_reuse_empty_conversation(
            self.workspace.id
        )
        self.effect_log = EffectLog(Path(self._temp.name) / "logs")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _tool(self, backend) -> RunShellTool:
        return RunShellTool(
            self.resolver,
            self.effect_log,
            execution_backend=backend,
            sandbox_network_mode=NetworkMode.DENY,
        )

    def test_backend_receives_workspace_write_policy(self) -> None:
        backend = FakeExecutionEnvironment(
            process_result=ProcessResult(
                exit_code=0, stdout="inside\n", stderr="", receipt=_receipt()
            )
        )

        result = asyncio.run(
            self._tool(backend).execute(
                _call(self.conversation.id, "echo inside"), CancellationToken()
            )
        )

        self.assertEqual(1, len(backend.processes))
        request = backend.processes[0]
        self.assertEqual(("/bin/bash", "-lc", "echo inside"), request.argv)
        self.assertEqual(str(self.root.resolve()), request.cwd)
        self.assertEqual(str(self.root.resolve()), request.policy.workspace_root)
        self.assertEqual((), request.policy.write_allow_paths)
        self.assertEqual(NetworkMode.DENY, request.policy.network_mode)
        self.assertNotIn("DEEPSEEK_API_KEY", request.environment)
        self.assertIn("inside", result.content)
        self.assertEqual(0, result.structured_content["exitCode"])

    def test_nonzero_exit_is_passed_through(self) -> None:
        backend = FakeExecutionEnvironment(
            process_result=ProcessResult(
                exit_code=3,
                stdout="",
                stderr="oops\n",
                receipt=_receipt(),
            )
        )

        result = asyncio.run(
            self._tool(backend).execute(
                _call(self.conversation.id, "exit 3"), CancellationToken()
            )
        )

        self.assertEqual(3, result.structured_content["exitCode"])
        self.assertIn("退出码 3", result.content)

    def test_missing_exit_code_is_reported_as_timeout(self) -> None:
        backend = FakeExecutionEnvironment(
            process_result=ProcessResult(
                exit_code=None, stdout="", stderr="", receipt=_receipt()
            )
        )

        result = asyncio.run(
            self._tool(backend).execute(
                _call(self.conversation.id, "sleep 999"), CancellationToken()
            )
        )

        self.assertIsNone(result.structured_content["exitCode"])
        self.assertTrue(result.structured_content["timedOut"])

    def test_backend_failure_fails_closed_without_falling_back(self) -> None:
        class _Failing(FakeExecutionEnvironment):
            async def run_process(self, request):  # noqa: ANN001
                raise AgentPlatformError(
                    "sandbox_unavailable", "sandbox-exec 不可用。", retryable=False
                )

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                self._tool(_Failing()).execute(
                    _call(self.conversation.id, "echo x"), CancellationToken()
                )
            )

        self.assertEqual("sandbox_unavailable", ctx.exception.code)
        self.assertFalse((self.root / "x").exists())

    def test_without_backend_uses_direct_execution(self) -> None:
        tool = RunShellTool(self.resolver, self.effect_log)

        result = asyncio.run(
            tool.execute(
                _call(self.conversation.id, "printf direct"), CancellationToken()
            )
        )

        self.assertEqual("direct", result.content.strip())
        self.assertEqual(0, result.structured_content["exitCode"])


def _receipt():
    from endless_task.agent_platform import EffectOutcome, EffectReceipt

    return EffectReceipt(
        effect_id="effect_1",
        tool_call_id="call_sandbox",
        effect_type="process",
        target=".",
        started_at="2026-09-09T00:00:00Z",
        outcome=EffectOutcome.COMMITTED,
        backend="fake",
        safe_summary="fake",
        committed_at="2026-09-09T00:00:01Z",
    )


class ShellSandboxStartupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "startup.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _build(self, env: dict[str, str]):
        from endless_task.api import AppSettings, create_app
        from endless_task.runtime import FakeProvider

        with mock.patch.dict(os.environ, env, clear=False):
            return create_app(
                settings=AppSettings(
                    database_path=self.database_path,
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )

    def test_unknown_mode_refuses_startup(self) -> None:
        with self.assertRaises(ValueError):
            self._build({"ENDLESS_TASK_SHELL_SANDBOX": "bogus"})

    def test_container_without_image_refuses_startup(self) -> None:
        with self.assertRaises(ValueError):
            self._build(
                {"ENDLESS_TASK_SHELL_SANDBOX": "container", "ENDLESS_TASK_SHELL_SANDBOX_IMAGE": ""}
            )

    def test_seatbelt_unavailable_refuses_startup(self) -> None:
        with mock.patch(
            "endless_task.execution_env.probe_seatbelt", return_value=False
        ):
            with self.assertRaises(ValueError):
                self._build({"ENDLESS_TASK_SHELL_SANDBOX": "seatbelt"})

    def test_off_keeps_direct_execution(self) -> None:
        app = self._build({"ENDLESS_TASK_SHELL_SANDBOX": "off"})
        tool = app.state.container.tool_registry.resolve("run_shell")
        self.assertIsNone(getattr(tool, "_execution_backend", None))


if __name__ == "__main__":
    unittest.main()
