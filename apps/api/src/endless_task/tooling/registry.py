from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Tuple

if TYPE_CHECKING:
    from endless_task.runtime.cancellation import CancellationToken

from .protocol import ToolCall, ToolDefinition, ToolResult, ToolValidationError


class RegisteredTool(Protocol):
    definition: ToolDefinition

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        ...

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        """参数级恒确认判定：返回 True 时即使提权模式也弹审批（如删除/危险命令）。"""
        return False


class ToolRegistry:
    """An immutable-by-name registry; execution orchestration belongs to R1.1."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ToolValidationError(
                "duplicate_tool",
                f"Tool is already registered: {name}",
            )
        self._tools[name] = tool

    def replace_tools(
        self,
        tools: Tuple[RegisteredTool, ...],
        *,
        namespace: str = "mcp__",
    ) -> None:
        """Atomically replace every tool in a dynamic namespace."""
        replacements: dict[str, RegisteredTool] = {}
        for tool in tools:
            name = tool.definition.name
            if not name.startswith(namespace):
                raise ToolValidationError(
                    "invalid_namespace",
                    f"Tool does not belong to namespace {namespace}: {name}",
                )
            if name in replacements:
                raise ToolValidationError(
                    "duplicate_tool",
                    f"Tool is already registered: {name}",
                )
            replacements[name] = tool
        self._tools = {
            name: tool
            for name, tool in self._tools.items()
            if not name.startswith(namespace)
        }
        self._tools.update(replacements)

    def remove_tools(self, *, namespace: str = "mcp__") -> None:
        self._tools = {
            name: tool
            for name, tool in self._tools.items()
            if not name.startswith(namespace)
        }

    def resolve(self, name: str) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolValidationError("unknown_tool", f"Unknown tool: {name}")
        return tool

    def definitions(self) -> Tuple[ToolDefinition, ...]:
        return tuple(self._tools[name].definition for name in sorted(self._tools))
