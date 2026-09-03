from __future__ import annotations

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolActivityCopy,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
)

from .models import FileError
from .protocol import TextFileRepository


class ReadTextFileTool:
    definition = ToolDefinition(
        name="read_text_file",
        description=(
            "读取当前会话中用户已上传的 UTF-8 文本文件。"
            "可按行分段读取；回答时必须引用结果提供的来源标签。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        timeout_seconds=5.0,
        max_output_characters=20_000,
    )

    def __init__(self, repository: TextFileRepository) -> None:
        self._repository = repository

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        try:
            name = self._repository.get_file(
                conversation_id=call.conversation_id,
                file_id=call.require_argument("file_id", str),
            ).metadata.original_name
        except (FileError, KeyError):
            name = "已上传文档"
        return ToolActivityCopy(
            running=f"正在读取 {name}",
            completed=f"已读取 {name}",
            failed=f"读取 {name} 失败，可以重试",
            cancelled=f"已停止读取 {name}",
        )

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        cancellation_token.raise_if_cancelled()
        try:
            stored = self._repository.get_file(
                conversation_id=call.conversation_id,
                file_id=call.require_argument("file_id", str),
            )
        except FileError as error:
            raise ToolError(
                error.code,
                error.safe_message,
                retryable=False,
            ) from error

        lines = stored.content.splitlines()
        if not lines:
            lines = [""]
        start_line = call.optional_argument("start_line", int, 1)
        line_count = call.optional_argument("line_count", int, 120)
        if start_line > len(lines):
            raise ToolError(
                "file_line_out_of_range",
                f"文件只有 {len(lines)} 行，无法从第 {start_line} 行读取。",
                retryable=False,
            )
        end_line = min(len(lines), start_line + line_count - 1)
        selected = "\n".join(lines[start_line - 1 : end_line])
        source_label = f"{stored.metadata.original_name}:L{start_line}-L{end_line}"
        content = f"[来源：{source_label}]\n{selected}"
        cancellation_token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=content,
            structured_content={
                "fileId": stored.metadata.id,
                "fileName": stored.metadata.original_name,
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": len(lines),
                "sourceLabel": source_label,
            },
        )
