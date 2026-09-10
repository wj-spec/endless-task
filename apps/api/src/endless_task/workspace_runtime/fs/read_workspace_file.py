"""`ReadWorkspaceFileTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations

from ..path_safety import resolve_read_path_with_variants
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import ToolActivityCopy, ToolApprovalMode, ToolCall, ToolDefinition, ToolEffect, ToolError, ToolResult
from .common import _decode_utf8


class ReadWorkspaceFileTool:
    definition = ToolDefinition(
        name="read_workspace_file",
        description=(
            "读取当前工作区内文件的 UTF-8 文本内容。path 为相对工作区根的路径。"
            "start_line 指定起始行（默认 1）；line_count 指定最多读多少行（不传时自动"
            "按单次输出预算读完尽可能多，超预算会在末尾提示续读行号，模型应据提示继续"
            "调用直到读到文件末尾）；回答时必须引用结果提供的来源标签。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=40_000,
    )

    # 内容截断后为续读提示预留的字符预算。
    _RESUME_HINT_BUDGET = 320

    def __init__(
        self,
        resolver,
        *,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._resolver = resolver
        self._max_file_bytes = max_file_bytes

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在读取工作区文件",
            completed="已读取工作区文件",
            failed="读取工作区文件失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        resolved = resolve_read_path_with_variants(
            binding.root, call.require_argument("path", str)
        )
        if not resolved.canonical.exists():
            raise ToolError(
                "path_not_found",
                f"文件不存在：{resolved.original_raw}。",
                retryable=False,
            )
        if not resolved.canonical.is_file():
            raise ToolError(
                "path_is_directory",
                "路径指向目录，请指定文件。",
                retryable=False,
            )
        start_line = call.optional_argument("start_line", int, 1)
        # 显式 line_count 限制行数；未传时按预算自动读取（见 _slice_with_budget）。
        explicit_count = call.optional_argument("line_count", int, None)
        file_size = resolved.canonical.stat().st_size
        if file_size <= self._max_file_bytes:
            return self._read_bounded(
                call,
                resolved,
                start_line=start_line,
                explicit_count=explicit_count,
            )
        # 大文件（超过 max_file_bytes）：流式按需窗口读取，不整读进内存；
        # totalLines 无法廉价获得（返回 None）。
        return self._read_large_window(
            call,
            resolved,
            start_line=start_line,
            explicit_count=explicit_count,
        )

    # ---- 小文件（≤ max_file_bytes）：整读 + 精确 totalLines ----

    def _read_bounded(
        self,
        call: ToolCall,
        resolved,
        *,
        start_line: int,
        explicit_count: int | None,
    ) -> ToolResult:
        raw = resolved.canonical.read_bytes()
        text = _decode_utf8(raw)
        lines = text.splitlines() or [""]
        total_lines = len(lines)
        if start_line > total_lines:
            raise ToolError(
                "file_line_out_of_range",
                f"文件只有 {total_lines} 行，无法从第 {start_line} 行读取。",
                retryable=False,
            )
        prefix = f"[来源：{self._label(resolved, start_line, total_lines)}]\n"
        selected, taken_count, truncated, resume = self._slice_with_budget(
            lines[start_line - 1 :],
            start_line=start_line,
            explicit_count=explicit_count,
            prefix=prefix,
        )
        end_line = start_line + taken_count - 1 if taken_count else start_line
        content = prefix + selected
        if truncated and resume is not None:
            content += (
                f"[内容已截断：仅显示至 L{end_line}。继续读取请调用 read_workspace_file "
                f"path={resolved.original_raw} start_line={resume}]"
            )
        return ToolResult(
            tool_call_id=call.id,
            content=content,
            structured_content={
                "path": resolved.original_raw,
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": total_lines,
                "variant": resolved.variant,
                "truncated": truncated,
                **({"resumeStartLine": resume} if resume is not None else {}),
            },
        )

    # ---- 大文件（> max_file_bytes）：流式窗口，不整读 ----

    def _read_large_window(
        self,
        call: ToolCall,
        resolved,
        *,
        start_line: int,
        explicit_count: int | None,
    ) -> ToolResult:
        budget = (
            self.definition.max_output_characters
            - self._RESUME_HINT_BUDGET
            - len(f"[来源：工作区文件 {resolved.original_raw}:L{start_line}-L∞]\n")
        )
        taken: list[str] = []
        used = 0
        truncated = False
        resume: int | None = None
        consumed = 0  # 已扫过的绝对行数（含跳过的前缀行）
        reached_target = False
        with resolved.canonical.open("r", encoding="utf-8", newline=None) as handle:
            for raw_line in handle:
                consumed += 1
                if consumed < start_line:
                    continue
                reached_target = True
                line = raw_line.rstrip("\n").rstrip("\r")
                cost = len(line)
                if used + cost > budget:
                    truncated = True
                    resume = consumed
                    break
                if cost > budget:
                    # 单行即超预算：截断该行本身，续读从下一行开始。
                    taken.append(line[: max(1, budget)])
                    truncated = True
                    resume = consumed + 1
                    break
                taken.append(line)
                used += cost
                if explicit_count is not None and len(taken) >= explicit_count:
                    break
            if not reached_target:
                raise ToolError(
                    "file_line_out_of_range",
                    f"文件未到达第 {start_line} 行（当前仅 {consumed} 行可读）；"
                    "请先降低 start_line 或用 workspace_search 定位内容。",
                    retryable=False,
                )
        selected = "\n".join(taken)
        end_line = start_line + len(taken) - 1 if taken else start_line
        prefix = f"[来源：{self._label(resolved, start_line, end_line)}]\n"
        content = prefix + selected
        if truncated and resume is not None:
            content += (
                f"[内容已截断：仅显示至 L{end_line}。继续读取请调用 read_workspace_file "
                f"path={resolved.original_raw} start_line={resume}]"
            )
        return ToolResult(
            tool_call_id=call.id,
            content=content,
            structured_content={
                "path": resolved.original_raw,
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": None,
                "variant": resolved.variant,
                "truncated": truncated,
                "largeFile": True,
                **({"resumeStartLine": resume} if resume is not None else {}),
            },
        )

    @staticmethod
    def _label(resolved, start_line: int, end_line: int) -> str:
        return (
            f"工作区文件 {resolved.original_raw}:L{start_line}-L{end_line}"
            + (f"（实际路径 {resolved.canonical}，macOS 变体 {resolved.variant}）"
               if resolved.variant != "exact" else "")
        )

    @staticmethod
    def _slice_with_budget(
        lines: list[str],
        *,
        start_line: int,
        explicit_count: int | None,
        prefix: str,
    ) -> tuple[str, int, bool, int | None]:
        """在单次输出预算内取行；未显式 line_count 时自动读满预算。

        返回 (selected_text, taken_count, truncated, resume_start_line)。
        resume_start_line 为 None 表示未截断（无续读需求）。
        """
        budget = (
            ReadWorkspaceFileTool.definition.max_output_characters
            - ReadWorkspaceFileTool._RESUME_HINT_BUDGET
            - len(prefix)
        )
        window = lines if explicit_count is None else lines[:explicit_count]
        taken: list[str] = []
        used = 0
        truncated = False
        resume: int | None = None
        for index, line in enumerate(window):
            cost = len(line)
            if used + cost > budget and taken:
                truncated = True
                resume = start_line + index
                break
            if cost > budget:
                # 单行即超预算：截断该行本身，续读从下一行开始。
                taken.append(line[: max(1, budget)])
                truncated = True
                resume = start_line + index + 1
                break
            taken.append(line)
            used += cost
        # 显式 line_count 恰好在窗口末尾（还有更多行）不视为截断——用户指定了
        # 读取上限；仅当预算耗尽才提示续读。
        return "\n".join(taken), len(taken), truncated, resume
