"""`ListWorkspaceDirTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations


MAX_LIST_ITEMS = 200
from ..path_safety import resolve_workspace_path
from .common import kernel_list
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
        # 枚举走**内核句柄**（逐段 O_NOFOLLOW）：把目录换成指向区外的 symlink 后再列，
        # 会被拒 —— 而不是列出区外的目录内容。排序键保持原样：先按 is_file、再按小写名。
        for name, is_dir, is_file, is_symlink, size in sorted(
            kernel_list(binding, resolved),
            key=lambda entry: (entry[2], entry[0].lower()),
        ):
            if count >= MAX_LIST_ITEMS:
                lines.append("…（条目过多，已截断）")
                break
            # symlink 明确标为 link（不跟进去）：比"悄悄跟随并报目标大小"更诚实，
            # 也不会把区外目标的尺寸喂给模型。
            if is_dir:
                kind, suffix = "dir ", "/"
            elif is_symlink:
                kind, suffix = "link", ""
            else:
                kind, suffix = "file", ""
            shown_size = 0 if is_dir else size
            lines.append(f"{kind} {name}{suffix}  {shown_size} 字节")
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
