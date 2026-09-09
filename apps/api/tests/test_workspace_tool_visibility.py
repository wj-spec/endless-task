"""工作区工具可见性（v1/v2 共用）验收：无绑定会话对工作区工具不可见。

覆盖：
- workspace_tool_filter 在「未绑定 / resolver 缺失」时过滤工作区工具；
- 在「已绑定」时不过滤；
- ToolExecutionCoordinator.definitions(conversation_id) 按会话过滤；
- 集成回归：无绑定会话装配只读工具 + 工作区工具时，模型只能看到只读工具。
"""

from __future__ import annotations

import unittest
from typing import Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.execution import ToolExecutionCoordinator
from endless_task.workspace_runtime.fs_tools import ListWorkspaceDirTool
from endless_task.workspace_runtime.visibility import (
    WORKSPACE_TOOLS,
    workspace_tool_filter,
)


class _StubResolver:
    """Bare resolver stub：按 conversation_id 返回绑定或 None。"""

    def __init__(self, bound: set[str]) -> None:
        self._bound = bound

    def resolve_binding(self, conversation_id: str) -> Optional[object]:
        return object() if conversation_id in self._bound else None


class _RegistryStub:
    def __init__(self, tools: list[object]) -> None:
        self._tools = tools

    def definitions(self):
        # 与真实 ToolRegistry.definitions() 一致：返回 ToolDefinition 元组。
        return tuple(tool.definition for tool in self._tools)


class WorkspaceToolFilterTest(unittest.TestCase):
    def test_unbound_conversation_filters_workspace_tools(self) -> None:
        resolver = _StubResolver(bound=set())
        predicate = workspace_tool_filter(resolver, "conv_unbound")
        self.assertIsNotNone(predicate)
        for name in WORKSPACE_TOOLS:
            self.assertFalse(predicate(name))  # type: ignore[union-attr]
        # 非工作区工具保持可见。
        self.assertTrue(predicate("read_text_file"))  # type: ignore[union-attr]

    def test_bound_conversation_does_not_filter(self) -> None:
        resolver = _StubResolver(bound={"conv_bound"})
        self.assertIsNone(workspace_tool_filter(resolver, "conv_bound"))

    def test_missing_resolver_does_not_filter(self) -> None:
        self.assertIsNone(workspace_tool_filter(None, "conv_any"))

    def test_workspace_tools_set_matches_v1_surface(self) -> None:
        # v1/v2 共用的工作区工具集；S7 起新增 edit/manage 两个动词。
        # v1 未注册这两个名字，多过滤掉它们不影响 v1 行为。
        self.assertEqual(
            WORKSPACE_TOOLS,
            frozenset(
                {
                    "read_workspace_file",
                    "write_workspace_file",
                    "edit_workspace_file",
                    "manage_workspace_paths",
                    "list_workspace_dir",
                    "delete_workspace_file",
                    "run_shell",
                    "workspace_search",
                }
            ),
        )


class ToolVisibilityDefinitionsTest(unittest.TestCase):
    def _coordinator(self, resolver) -> ToolExecutionCoordinator:
        registry = _RegistryStub(
            [
                ListWorkspaceDirTool(resolver),
                _ReadOnlyToolStub(),
            ]
        )
        return ToolExecutionCoordinator(
            repository=None,  # type: ignore[arg-type]
            tool_registry=registry,  # type: ignore[arg-type]
            tool_filter_provider=lambda cid: workspace_tool_filter(resolver, cid),
        )

    def test_unbound_conversation_excludes_workspace_tools(self) -> None:
        resolver = _StubResolver(bound=set())
        coordinator = self._coordinator(resolver)
        names = {item.name for item in coordinator.definitions("conv_unbound")}
        self.assertNotIn("list_workspace_dir", names)
        self.assertIn("read_only", names)

    def test_bound_conversation_includes_workspace_tools(self) -> None:
        resolver = _StubResolver(bound={"conv_bound"})
        coordinator = self._coordinator(resolver)
        names = {item.name for item in coordinator.definitions("conv_bound")}
        self.assertIn("list_workspace_dir", names)
        self.assertIn("read_only", names)


class _ReadOnlyToolStub:
    def __init__(self) -> None:
        from endless_task.tooling import (
            ToolApprovalMode,
            ToolDefinition,
            ToolEffect,
            ToolResult,
        )

        self.definition = ToolDefinition(
            name="read_only",
            description="read-only stub",
            input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=2.0,
        )

    async def execute(self, call, cancellation_token: CancellationToken) -> object:
        del call, cancellation_token
        from endless_task.tooling import ToolResult

        return ToolResult(tool_call_id="x", content="ok")


if __name__ == "__main__":
    unittest.main()
