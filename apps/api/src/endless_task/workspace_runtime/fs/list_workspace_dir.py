"""`ListWorkspaceDirTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations


MAX_LIST_ITEMS = 200
from ..path_safety import resolve_workspace_path
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import ToolActivityCopy, ToolApprovalMode, ToolCall, ToolDefinition, ToolEffect, ToolError, ToolResult


class ListWorkspaceDirTool:
    definition = ToolDefinition(
        name="list_workspace_dir",
        description=(
            "列出工作区根内某个目录的直接子项（名字、类型、大小、可写性）。"
            "path 为相对工作区根的目录路径，省略表示根目录。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            "required": [],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=20_000,
    )

    def __init__(self, resolver) -> None:
        self._resolver = resolver

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在列出工作区目录",
            completed="已列出工作区目录",
            failed="列出工作区目录失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        raw_path = call.optional_argument("path", str, ".")
        resolved = resolve_workspace_path(binding.root, raw_path)
        if not resolved.canonical.exists():
            raise ToolError(
                "path_not_found",
                f"目录不存在：{resolved.original_raw}。",
                retryable=False,
            )
        if not resolved.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "路径指向文件，请指定目录。",
                retryable=False,
            )
        lines = []
        count = 0
        for entry in sorted(
            resolved.canonical.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        ):
            if count >= MAX_LIST_ITEMS:
                lines.append("…（条目过多，已截断）")
                break
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            kind = "dir " if is_dir else "file"
            suffix = "/" if is_dir else ""
            lines.append(f"{kind} {entry.name}{suffix}  {size} 字节")
            count += 1
        content = "\n".join(lines) if lines else "（空目录）"
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"[目录：{resolved.original_raw}]\n{content}",
            structured_content={
                "path": resolved.original_raw,
                "itemCount": count,
            },
        )
