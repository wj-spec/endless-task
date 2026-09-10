"""`EditWorkspaceFileTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations

from ..effect_log import EffectLog, EffectReceipt, sha256_text
from ..path_safety import resolve_workspace_path
from ..resolver import WorkspaceBinding
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolActivityCopy,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
)
from .common import _PATH_LOCKS, _decode_utf8, _ensure_checkpoint, _line_diff, _log_effect, _match_line_numbers, _now_iso, _record_mutation, _record_undo_write, _sha256_bytes


class EditWorkspaceFileTool:
    """就地编辑：old_string → new_string（默认要求唯一匹配），返回行级 diff。

    为什么需要它：整文件重写既费 token 又容易误伤无关内容；有了唯一匹配的
    字符串替换，模型改一处只需传上下文与替换文本（对齐参考实现的
    `str_replace_editor` 契约）。
    """

    definition = ToolDefinition(
        name="edit_workspace_file",
        description=(
            "在工作区根内就地修改一个 UTF-8 文本文件：把 old_string 替换为 "
            "new_string。old_string 必须与文件内容完全一致（含空白与缩进）；"
            "默认要求它在文件中唯一出现，出现 0 次或多次都会被拒绝——"
            "请带上足够上下文让它唯一，或显式设置 replace_all=true 替换全部。"
            "整文件新建/覆盖请用 write_workspace_file。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "old_string": {"type": "string", "minLength": 1, "maxLength": 20_000},
                "new_string": {"type": "string", "maxLength": 20_000},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=15.0,
        max_output_characters=6_000,
    )

    _DIFF_MAX_LINES = 120

    def __init__(
        self,
        resolver,
        effect_log: EffectLog,
        *,
        max_write_bytes: int = 512_000,
        checkpoint_coordinator=None,
        undo_service=None,
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._max_write_bytes = max_write_bytes
        self._checkpoint_coordinator = checkpoint_coordinator
        self._undo_service = undo_service

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在编辑工作区文件",
            completed="已编辑工作区文件",
            failed="编辑工作区文件失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        target = call.require_argument("path", str)
        return ToolApprovalPrompt(
            summary="允许编辑工作区文件吗？",
            reason=f"将在工作区文件 {target} 中替换一段文本（可撤销）。",
            metadata={"toolName": "edit_workspace_file", "path": target},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        path = call.require_argument("path", str)
        old_string = call.require_argument("old_string", str)
        new_string = call.optional_argument("new_string", str, "")
        replace_all = bool(call.optional_argument("replace_all", bool, False))
        resolved = resolve_workspace_path(binding.root, path)
        if not resolved.canonical.exists():
            raise ToolError(
                "path_not_found",
                f"文件不存在：{resolved.original_raw}（新建请用 write_workspace_file）。",
                retryable=False,
            )
        if resolved.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "路径指向目录，不能编辑。",
                retryable=False,
            )
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_content = _decode_utf8(resolved.canonical.read_bytes())
            occurrences = before_content.count(old_string)
            if occurrences == 0:
                raise ToolError(
                    "edit_no_match",
                    "old_string 在文件中没有出现；请核对空白/缩进，或先用 "
                    "read_workspace_file 确认内容。",
                    retryable=False,
                )
            if occurrences > 1 and not replace_all:
                line_numbers = _match_line_numbers(before_content, old_string)
                preview = ", ".join(f"L{n}" for n in line_numbers[:10])
                raise ToolError(
                    "edit_not_unique",
                    f"old_string 出现了 {occurrences} 次（{preview}）；"
                    "请带上足够上下文让它唯一，或设置 replace_all=true 全部替换。",
                    retryable=False,
                )
            if replace_all:
                after_content = before_content.replace(old_string, new_string)
            else:
                after_content = before_content.replace(old_string, new_string, 1)
            if after_content == before_content:
                raise ToolError(
                    "edit_no_change",
                    "替换后内容没有变化。",
                    retryable=False,
                )
            encoded = after_content.encode("utf-8")
            if len(encoded) > self._max_write_bytes:
                raise ToolError(
                    "write_too_large",
                    f"编辑后内容超过写入上限（{self._max_write_bytes} 字节）。",
                    retryable=False,
                )
            self._ensure_checkpoint(call, binding)
            resolved.canonical.write_text(after_content, encoding="utf-8")
        diff_lines = _line_diff(
            before_content, after_content, max_lines=self._DIFF_MAX_LINES
        )
        _record_undo_write(
            self._undo_service,
            call,
            binding,
            resolved,
            before_content=before_content,
            after_hash=sha256_text(after_content),
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            resolved,
            operation="file_edit",
            before_hash=_sha256_bytes(before_content.encode("utf-8")),
            after_hash=_sha256_bytes(encoded),
        )
        receipt = EffectReceipt(
            kind="file_write",
            path=str(resolved.canonical),
            sha256=sha256_text(after_content),
            executed_at=_now_iso(),
        )
        _log_effect(
            self._effect_log,
            call,
            binding,
            "edit_file",
            resolved.original_raw,
            receipt,
        )
        token.raise_if_cancelled()
        replaced = occurrences if replace_all else 1
        diff_text = "\n".join(diff_lines)
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已编辑工作区文件：{resolved.original_raw}（替换 {replaced} 处）。\n"
                f"```diff\n{diff_text}\n```"
            ),
            structured_content={
                "path": resolved.original_raw,
                "replaced": replaced,
                "diff": diff_lines,
                "bytes": len(encoded),
                "effect": receipt.as_dict(),
            },
        )

    def _ensure_checkpoint(self, call: ToolCall, binding: WorkspaceBinding) -> None:
        _ensure_checkpoint(self._checkpoint_coordinator, call, binding)
