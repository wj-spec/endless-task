"""R5.12 工作区运行时验收：路径安全、fs/shell 工具、副作用日志、绑定校验、恒确认与工具过滤。"""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import PermissionMode
from endless_task.domain.repositories import ConflictError, ValidationError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
    ToolError,
)
from endless_task.workspace_runtime.dangerous_commands import is_dangerous
from endless_task.workspace_runtime.effect_log import EffectLog, EffectReceipt
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    ListWorkspaceDirTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.path_safety import (
    resolve_read_path_with_variants,
    resolve_workspace_path,
    validate_bind_root,
)
from endless_task.workspace_runtime.resolver import WorkspaceResolver
from endless_task.workspace_runtime.shell_runner import (
    _sanitized_env,
    run_shell_command,
)
from endless_task.workspace_runtime.shell_tool import RunShellTool


def _call(conversation_id: str = "conv_1", **arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="tool",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-08-27T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


class PathSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_resolves_relative_path(self) -> None:
        resolved = resolve_workspace_path(self.root, "docs/plan.md")
        self.assertEqual(self.root.resolve() / "docs" / "plan.md", resolved.canonical)

    def test_rejects_absolute_path(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            resolve_workspace_path(self.root, "/etc/passwd")
        self.assertEqual("path_escape", ctx.exception.code)

    def test_rejects_parent_segments(self) -> None:
        for raw in ("../secret", "a/../../secret", ".."):
            with self.assertRaises(ToolError) as ctx:
                resolve_workspace_path(self.root, raw)
            self.assertEqual("path_escape", ctx.exception.code, raw)

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir)
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = self.root / "link"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ToolError) as ctx:
                resolve_workspace_path(self.root, "link/secret.txt")
            self.assertEqual("path_escape", ctx.exception.code)

    def test_macos_curly_quote_read_variant(self) -> None:
        # macOS 截图等文件名含弯引号（U+2019），用户/模型常输入直引号（U+0027）
        file_with_curly = Path(self.root) / "it\u2019s.txt"
        file_with_curly.write_text("内容", encoding="utf-8")
        resolved = resolve_read_path_with_variants(self.root, "it's.txt")
        self.assertTrue(resolved.canonical.exists())
        self.assertEqual("curly", resolved.variant)

    def test_validate_bind_root_rejects_home_and_root(self) -> None:
        self.assertIsNotNone(validate_bind_root(Path.home()))
        self.assertIsNotNone(validate_bind_root(Path("/")))
        self.assertIsNone(validate_bind_root(self.root))


class WorkspaceBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "runtime.db")
        self.database.initialize()
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.root = Path(self._temp.name) / "project"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_bind_and_unbind(self) -> None:
        workspace = self.workspaces.create_workspace("项目")
        bound = self.workspaces.bind_root_path(workspace.id, str(self.root))
        self.assertEqual(str(self.root.resolve()), bound.root_path)
        unbound = self.workspaces.unbind_root_path(workspace.id)
        self.assertIsNone(unbound.root_path)

    def test_rejects_sensitive_root(self) -> None:
        workspace = self.workspaces.create_workspace("项目")
        with self.assertRaises(ValidationError):
            self.workspaces.bind_root_path(workspace.id, str(Path.home()))

    def test_rejects_duplicate_binding(self) -> None:
        first = self.workspaces.create_workspace("A")
        second = self.workspaces.create_workspace("B")
        self.workspaces.bind_root_path(first.id, str(self.root))
        with self.assertRaises(ConflictError):
            self.workspaces.bind_root_path(second.id, str(self.root))


class FsToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "fs.db")
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

    def test_read_workspace_file_with_source_label(self) -> None:
        (self.root / "README.md").write_text(
            "\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8"
        )
        tool = ReadWorkspaceFileTool(self.resolver)
        result = asyncio.run(tool.execute(_call(self.conversation.id, path="README.md"), _token()))
        self.assertIn("[来源：工作区文件 README.md:L1-L10]", result.content)
        self.assertEqual(10, result.structured_content["totalLines"])

    def test_write_then_read_roundtrip(self) -> None:
        tool = WriteWorkspaceFileTool(self.resolver, self.effect_log)
        result = asyncio.run(
            tool.execute(_call(self.conversation.id, path="docs/plan.md", content="计划内容"), _token())
        )
        self.assertEqual("file_write", result.structured_content["effect"]["kind"])
        self.assertTrue((self.root / "docs" / "plan.md").exists())
        reader = ReadWorkspaceFileTool(self.resolver)
        read = asyncio.run(reader.execute(_call(self.conversation.id, path="docs/plan.md"), _token()))
        self.assertIn("计划内容", read.content)

    def test_write_rejects_escape(self) -> None:
        tool = WriteWorkspaceFileTool(self.resolver, self.effect_log)
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(
                tool.execute(_call(self.conversation.id, path="../evil.txt", content="x"), _token())
            )
        self.assertEqual("path_escape", ctx.exception.code)

    def test_delete_requires_explicit_confirmation(self) -> None:
        target = self.root / "gone.txt"
        target.write_text("x", encoding="utf-8")
        tool = DeleteWorkspaceFileTool(self.resolver, self.effect_log)
        self.assertTrue(
            tool.requires_explicit_confirmation(_call(self.conversation.id, path="gone.txt"))
        )
        result = asyncio.run(tool.execute(_call(self.conversation.id, path="gone.txt"), _token()))
        self.assertEqual("file_delete", result.structured_content["effect"]["kind"])
        self.assertFalse(target.exists())

    def test_list_workspace_dir(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "sub").mkdir()
        tool = ListWorkspaceDirTool(self.resolver)
        result = asyncio.run(tool.execute(_call(self.conversation.id), _token()))
        self.assertIn("a.txt", result.content)
        self.assertIn("sub/", result.content)

    def test_unbound_workspace_raises(self) -> None:
        self.workspaces.unbind_root_path(self.workspace.id)
        tool = ReadWorkspaceFileTool(self.resolver)
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(tool.execute(_call(self.conversation.id, path="README.md"), _token()))
        self.assertEqual("workspace_not_bound", ctx.exception.code)

    def test_run_shell_tool_returns_result_and_receipt(self) -> None:
        tool = RunShellTool(self.resolver, self.effect_log)
        result = asyncio.run(
            tool.execute(
                _call(self.conversation.id, command="printf shell-ok"),
                _token(),
            )
        )
        self.assertEqual("shell-ok", result.content)
        self.assertEqual(0, result.structured_content["exitCode"])
        self.assertEqual("shell", result.structured_content["effect"]["kind"])


class ShellRunnerTest(unittest.TestCase):
    def test_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_shell_command(command="echo hello", cwd=Path(tmp)))
        self.assertEqual(0, result.exit_code)
        self.assertIn("hello", result.stdout)

    def test_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_shell_command(command="exit 3", cwd=Path(tmp)))
        self.assertEqual(3, result.exit_code)

    def test_hard_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_shell_command(
                    command="sleep 5",
                    cwd=Path(tmp),
                    timeout_seconds=1,
                    no_change_timeout_seconds=60,
                )
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.no_change_timeout)

    def test_no_change_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_shell_command(
                    command="sleep 5",
                    cwd=Path(tmp),
                    timeout_seconds=30,
                    no_change_timeout_seconds=0.5,
                )
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.no_change_timeout)

    def test_output_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_shell_command(
                    command="python3 -c 'print(\"x\" * 5000)'",
                    cwd=Path(tmp),
                    max_output_bytes=1024,
                )
            )
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 1024)

    def test_credential_env_sanitized(self) -> None:
        os.environ["R512_TEST_TOKEN"] = "secret-value"
        env = _sanitized_env()
        self.assertNotIn("R512_TEST_TOKEN", env)
        os.environ.pop("R512_TEST_TOKEN", None)

    def test_dangerous_commands(self) -> None:
        self.assertTrue(is_dangerous("rm -rf build"))
        self.assertTrue(is_dangerous("git push origin main"))
        self.assertTrue(is_dangerous("curl -X POST http://example.com"))
        self.assertTrue(is_dangerous("sudo rm -rf /"))
        self.assertFalse(is_dangerous("ls -la"))
        self.assertFalse(is_dangerous("python3 build.py"))

    def test_shell_tool_dangerous_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = EffectLog(Path(tmp) / "logs")
            tool = RunShellTool(WorkspaceResolverStub(), log)
            self.assertTrue(
                tool.requires_explicit_confirmation(_call(command="rm -rf build"))
            )
            self.assertFalse(
                tool.requires_explicit_confirmation(_call(command="ls -la"))
            )


class WorkspaceResolverStub:
    def require_binding(self, conversation_id: str):
        raise AssertionError("should not reach resolver in this test")


class EffectLogTest(unittest.TestCase):
    def test_append_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = EffectLog(Path(tmp) / "logs")
            receipt = EffectReceipt(
                kind="file_write", path="/tmp/x", sha256="abc", executed_at="t0"
            )
            log.append(
                conversation_id="conv_1",
                workspace_id="ws_1",
                workspace_root="/tmp",
                operation="write_file",
                detail="x",
                receipt=receipt,
            )
            entries = log.list_for_workspace("ws_1")
            self.assertEqual(1, len(entries))
            self.assertEqual("file_write", entries[0]["receipt"]["kind"])
            self.assertEqual([], log.list_for_workspace("ws_other"))


if __name__ == "__main__":
    unittest.main()
