"""Execution parity for legacy built-in tools through LegacyToolAdapter.

AP-105a closing item. ``LegacyToolAdapter`` delegates to the very same
``RegisteredTool.execute`` the legacy coordinator calls (structural parity),
so these tests lock the observable outcome mapping for real tools:

- read-only tools (workspace file, directory listing, skill file) must yield
  byte-identical content/structured results on both paths,
- effectful workspace tools (write/delete) carry timestamps in their effect
  receipts, so parity is asserted on side effects and stable receipt fields
  instead of wall-clock values.

Shell, artifact and plan tools are excluded here: their execution paths are
approval- and repository-heavy and covered by their own suites; outcome
parity for them lands with the AP-107 golden-trajectory harness.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Optional

from endless_task.agent_platform import plain_json
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import LegacyToolAdapter, ToolExecutionRequest
from endless_task.tooling import (
    ToolCall,
    ToolCallStatus,
)
from endless_task.workspace_runtime import WorkspaceBinding
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    ListWorkspaceDirTool,
    ReadSkillFileTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)

CONVERSATION_ID = "conversation_1"


class BindingResolver:
    def __init__(self, binding: Optional[WorkspaceBinding]) -> None:
        self._binding = binding

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        del conversation_id
        if self._binding is None:
            raise AssertionError("Unexpected unbound conversation in parity test")
        return self._binding

    def resolve_binding(self, conversation_id: str) -> Optional[WorkspaceBinding]:
        del conversation_id
        return self._binding


def legacy_call(tool_name: str, arguments: Mapping, call_id: str = "call_x") -> ToolCall:
    return ToolCall(
        id=call_id,
        conversation_id=CONVERSATION_ID,
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name=tool_name,
        arguments=dict(arguments),
        status=ToolCallStatus.CREATED,
        created_at="2026-09-03T00:00:00Z",
    )


def adapter_request(tool_name: str, arguments: Mapping) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id="call_x",
        tool_name=tool_name,
        arguments=dict(arguments),
        conversation_id=CONVERSATION_ID,
        run_id="run_1",
        model_turn_id="model_turn_1",
        correlation_id="correlation_1",
        cancellation=CancellationToken(),
        created_at="2026-09-03T00:00:00Z",
        legacy_turn_id="turn_1",
        legacy_response_variant_id="variant_1",
    )


class LegacyExecutionParityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir(parents=True)
        self.logs = Path(self._tmp.name) / "logs"
        self.binding = WorkspaceBinding(workspace_id="workspace_1", root=self.root)
        self.resolver = BindingResolver(self.binding)
        self.effect_log = EffectLog(self.logs)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _run_both(self, tool, arguments: Mapping) -> tuple:
        legacy_result = await tool.execute(
            legacy_call(tool.definition.name, arguments),
            CancellationToken(),
        )
        adapter = LegacyToolAdapter(tool)
        adapter_outcome = await adapter.execute(
            adapter_request(tool.definition.name, arguments)
        )
        return legacy_result, adapter_outcome

    async def test_read_workspace_file_parity_is_identical(self) -> None:
        (self.root / "notes.md").write_text(
            "alpha\nbeta\ngamma\ndelta", encoding="utf-8"
        )
        tool = ReadWorkspaceFileTool(self.resolver)
        legacy, outcome = await self._run_both(
            tool, {"path": "notes.md", "start_line": 2, "line_count": 2}
        )
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual(legacy.content, outcome.content)
        self.assertEqual(
            plain_json(legacy.structured_content),
            plain_json(outcome.structured_content),
        )

    async def test_list_workspace_dir_parity_is_identical(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.md").write_text("b", encoding="utf-8")
        tool = ListWorkspaceDirTool(self.resolver)
        for arguments in ({}, {"path": "sub"}):
            with self.subTest(arguments=arguments):
                legacy, outcome = await self._run_both(tool, arguments)
                self.assertEqual("completed", outcome.status.value)
                self.assertEqual(legacy.content, outcome.content)
                self.assertEqual(
                    plain_json(legacy.structured_content),
                    plain_json(outcome.structured_content),
                )

    async def test_read_skill_file_parity_is_identical(self) -> None:
        skill_root = self.root / "skills"
        skill_root.mkdir()
        (skill_root / "demo.md").write_text("skill line one\nskill line two", encoding="utf-8")

        def skill_roots(conversation_id: str) -> tuple[Path, ...]:
            del conversation_id
            return (skill_root,)

        tool = ReadSkillFileTool(skill_roots)
        arguments = {
            "path": str(skill_root / "demo.md"),
            "start_line": 1,
            "line_count": 1,
        }
        legacy, outcome = await self._run_both(tool, arguments)
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual(legacy.content, outcome.content)
        self.assertEqual(
            plain_json(legacy.structured_content),
            plain_json(outcome.structured_content),
        )

    async def test_write_workspace_file_parity_on_side_effects(self) -> None:
        tool = WriteWorkspaceFileTool(self.resolver, self.effect_log)
        content = "写入内容 line1\nline2"
        legacy, legacy_outcome = await self._run_both(
            tool, {"path": "notes/a.txt", "content": content}
        )
        second = WriteWorkspaceFileTool(self.resolver, self.effect_log)
        _, adapter_outcome = await self._run_both(
            second, {"path": "notes/b.txt", "content": content}
        )
        self.assertEqual("completed", legacy_outcome.status.value)
        self.assertEqual("completed", adapter_outcome.status.value)
        self.assertTrue((self.root / "notes" / "a.txt").exists())
        self.assertTrue((self.root / "notes" / "b.txt").exists())
        self.assertEqual(content, (self.root / "notes" / "a.txt").read_text(encoding="utf-8"))
        self.assertEqual(content, (self.root / "notes" / "b.txt").read_text(encoding="utf-8"))
        legacy_effect = legacy.structured_content["effect"]
        adapter_effect = dict(adapter_outcome.structured_content or {})["effect"]
        self.assertEqual("file_write", legacy_effect["kind"])
        self.assertEqual(legacy_effect["sha256"], adapter_effect["sha256"])
        self.assertIn("executedAt", legacy_effect)

    async def test_delete_workspace_file_parity_on_side_effects(self) -> None:
        (self.root / "keep_legacy.txt").write_text("x", encoding="utf-8")
        (self.root / "keep_adapter.txt").write_text("x", encoding="utf-8")
        legacy_tool = DeleteWorkspaceFileTool(self.resolver, self.effect_log)
        legacy = await legacy_tool.execute(
            legacy_call("delete_workspace_file", {"path": "keep_legacy.txt"}),
            CancellationToken(),
        )
        adapter_tool = DeleteWorkspaceFileTool(self.resolver, self.effect_log)
        adapter_outcome = await LegacyToolAdapter(adapter_tool).execute(
            adapter_request("delete_workspace_file", {"path": "keep_adapter.txt"})
        )
        self.assertEqual("completed", adapter_outcome.status.value)
        self.assertFalse((self.root / "keep_legacy.txt").exists())
        self.assertFalse((self.root / "keep_adapter.txt").exists())
        self.assertEqual("file_delete", legacy.structured_content["effect"]["kind"])
        self.assertEqual(
            "file_delete",
            dict(adapter_outcome.structured_content or {})["effect"]["kind"],
        )

    async def test_effect_log_is_written_on_both_paths(self) -> None:
        tool = WriteWorkspaceFileTool(self.resolver, self.effect_log)
        await self._run_both(tool, {"path": "logged.txt", "content": "log me"})
        log_files = list(self.logs.glob("effects_*.jsonl"))
        self.assertTrue(log_files, "effect log must be written by both paths")
        body = log_files[0].read_text(encoding="utf-8")
        self.assertIn("write_file", body)


if __name__ == "__main__":
    unittest.main()
