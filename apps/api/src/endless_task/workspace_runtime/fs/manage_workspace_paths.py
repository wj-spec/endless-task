"""`ManageWorkspacePathsTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations

from ..effect_log import EffectLog, EffectReceipt
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
from .common import _PATH_LOCKS, _ensure_checkpoint, _log_effect, _now_iso, _read_text_or_none, _record_mutation, _record_undo_delete, _record_undo_write, _sha256_bytes


class ManageWorkspacePathsTool:
    """路径操作：mkdir / copy / move（工作区根内，覆盖需确认）。"""

    definition = ToolDefinition(
        name="manage_workspace_paths",
        description=(
            "在工作区根内执行路径操作：operation=mkdir 创建目录（自动补父目录）；"
            "copy 复制文件到 to_path；move 移动/重命名文件到 to_path。"
            "目标已存在时默认拒绝，需显式 overwrite=true（会要求用户确认）。"
            "只处理文件，目录移动/复制请逐个文件进行或说明后由用户处理。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["mkdir", "copy", "move"],
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "to_path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "overwrite": {"type": "boolean"},
            },
            "required": ["operation", "path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=20.0,
        max_output_characters=4_000,
    )

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
        operation = call.optional_argument("operation", str, "")
        verb = {"mkdir": "创建目录", "copy": "复制文件", "move": "移动文件"}.get(
            operation, "修改工作区路径"
        )
        return ToolActivityCopy(
            running=f"正在{verb}",
            completed=f"已{verb}",
            failed=f"{verb}失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        # 覆盖已有文件是不可逆的高风险分支：即使 approval_mode=AUTO 也强制确认。
        return bool(call.optional_argument("overwrite", bool, False))

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        operation = call.optional_argument("operation", str, "")
        source = call.require_argument("path", str)
        target = call.optional_argument("to_path", str, "")
        return ToolApprovalPrompt(
            summary="允许覆盖已有工作区文件吗？",
            reason=(
                f"operation={operation} 会把 {source} 写到 {target}，"
                "而目标已存在且 overwrite=true，原内容将被替换（可撤销）。"
            ),
            metadata={
                "toolName": "manage_workspace_paths",
                "operation": operation,
                "path": source,
                "toPath": target,
            },
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        operation = call.require_argument("operation", str)
        if operation not in {"mkdir", "copy", "move"}:
            raise ToolError(
                "invalid_operation",
                f"不支持的操作：{operation}（可选 mkdir/copy/move）。",
                retryable=False,
            )
        overwrite = bool(call.optional_argument("overwrite", bool, False))
        source = resolve_workspace_path(binding.root, call.require_argument("path", str))
        if operation == "mkdir":
            return await self._mkdir(call, binding, source, token)
        to_path = call.require_argument("to_path", str)
        target = resolve_workspace_path(binding.root, to_path)
        if operation == "copy":
            return await self._copy(call, binding, source, target, overwrite, token)
        return await self._move(call, binding, source, target, overwrite, token)

    async def _mkdir(self, call, binding, source, token) -> ToolResult:
        if source.canonical.exists():
            if source.canonical.is_dir():
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"目录已存在：{source.original_raw}。",
                    structured_content={"path": source.original_raw, "created": False},
                )
            raise ToolError(
                "path_is_file",
                "同名文件已存在，无法创建目录。",
                retryable=False,
            )
        source.canonical.mkdir(parents=True, exist_ok=True)
        receipt = EffectReceipt(
            kind="file_write",
            path=str(source.canonical),
            executed_at=_now_iso(),
        )
        _log_effect(self._effect_log, call, binding, "mkdir", source.original_raw, receipt)
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"已创建目录：{source.original_raw}。",
            structured_content={
                "path": source.original_raw,
                "created": True,
                "effect": receipt.as_dict(),
            },
        )

    async def _copy(self, call, binding, source, target, overwrite, token) -> ToolResult:
        if not source.canonical.exists():
            raise ToolError(
                "path_not_found", f"源文件不存在：{source.original_raw}。", retryable=False
            )
        if source.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "目录复制暂不开放，请逐个文件复制。",
                retryable=False,
            )
        if source.canonical.stat().st_size > self._max_write_bytes:
            raise ToolError(
                "write_too_large",
                f"源文件超过复制上限（{self._max_write_bytes} 字节）。",
                retryable=False,
            )
        locks = sorted({str(source.canonical), str(target.canonical)})
        acquired = [await _PATH_LOCKS.acquire(key) for key in locks]
        for lock in acquired:
            await lock.acquire()
        try:
            if target.canonical.exists():
                if target.canonical.is_dir():
                    raise ToolError(
                        "path_is_directory",
                        "目标路径是目录，请给出完整文件路径。",
                        retryable=False,
                    )
                if not overwrite:
                    raise ToolError(
                        "target_exists",
                        f"目标已存在：{target.original_raw}（如需覆盖请设置 overwrite=true）。",
                        retryable=False,
                    )
                if _read_text_or_none(target.canonical) is None:
                    raise ToolError(
                        "binary_overwrite_unsupported",
                        "目标文件不是可撤销的 UTF-8 文本，拒绝覆盖。",
                        retryable=False,
                    )
            before_content = _read_text_or_none(target.canonical)
            before_exists = target.canonical.exists()
            self._ensure_checkpoint(call, binding)
            target.canonical.parent.mkdir(parents=True, exist_ok=True)
            raw = source.canonical.read_bytes()
            target.canonical.write_bytes(raw)
        finally:
            for lock in reversed(acquired):
                lock.release()
        _record_undo_write(
            self._undo_service,
            call,
            binding,
            target,
            before_content=before_content if before_exists else None,
            after_hash=_sha256_bytes(raw),
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            target,
            operation="file_write",
            before_hash=None,
            after_hash=_sha256_bytes(raw),
        )
        receipt = EffectReceipt(
            kind="file_write",
            path=str(target.canonical),
            sha256=_sha256_bytes(raw),
            executed_at=_now_iso(),
        )
        _log_effect(
            self._effect_log,
            call,
            binding,
            "copy_file",
            f"{source.original_raw} -> {target.original_raw}",
            receipt,
        )
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已复制文件：{source.original_raw} -> {target.original_raw}"
                f"（{len(raw)} 字节）。"
            ),
            structured_content={
                "operation": "copy",
                "path": source.original_raw,
                "toPath": target.original_raw,
                "bytes": len(raw),
                "effect": receipt.as_dict(),
            },
        )

    async def _move(self, call, binding, source, target, overwrite, token) -> ToolResult:
        if not source.canonical.exists():
            raise ToolError(
                "path_not_found", f"源文件不存在：{source.original_raw}。", retryable=False
            )
        if source.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "目录移动暂不开放，请逐个文件移动。",
                retryable=False,
            )
        if str(source.canonical) == str(target.canonical):
            raise ToolError(
                "no_op", "源路径与目标路径相同。", retryable=False
            )
        locks = sorted({str(source.canonical), str(target.canonical)})
        acquired = [await _PATH_LOCKS.acquire(key) for key in locks]
        for lock in acquired:
            await lock.acquire()
        try:
            if target.canonical.exists():
                if target.canonical.is_dir():
                    raise ToolError(
                        "path_is_directory",
                        "目标路径是目录，请给出完整文件路径。",
                        retryable=False,
                    )
                if not overwrite:
                    raise ToolError(
                        "target_exists",
                        f"目标已存在：{target.original_raw}（如需覆盖请设置 overwrite=true）。",
                        retryable=False,
                    )
                if _read_text_or_none(target.canonical) is None:
                    raise ToolError(
                        "binary_overwrite_unsupported",
                        "目标文件不是可撤销的 UTF-8 文本，拒绝覆盖。",
                        retryable=False,
                    )
            before_source = _read_text_or_none(source.canonical)
            before_target = _read_text_or_none(target.canonical)
            before_target_exists = target.canonical.exists()
            self._ensure_checkpoint(call, binding)
            target.canonical.parent.mkdir(parents=True, exist_ok=True)
            raw = source.canonical.read_bytes()
            target.canonical.write_bytes(raw)
            source.canonical.unlink()
        finally:
            for lock in reversed(acquired):
                lock.release()
        _record_undo_write(
            self._undo_service,
            call,
            binding,
            target,
            before_content=before_target if before_target_exists else None,
            after_hash=_sha256_bytes(raw),
        )
        _record_undo_delete(
            self._undo_service,
            call,
            binding,
            source,
            before_content=before_source,
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            source,
            operation="file_delete",
            before_hash=_sha256_bytes(raw),
            after_hash=None,
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            target,
            operation="file_write",
            before_hash=None,
            after_hash=_sha256_bytes(raw),
        )
        receipt = EffectReceipt(
            kind="file_write",
            path=str(target.canonical),
            sha256=_sha256_bytes(raw),
            executed_at=_now_iso(),
        )
        _log_effect(
            self._effect_log,
            call,
            binding,
            "move_file",
            f"{source.original_raw} -> {target.original_raw}",
            receipt,
        )
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已移动文件：{source.original_raw} -> {target.original_raw}"
                f"（{len(raw)} 字节）。撤销需要两步：先撤销对目标文件的写入，"
                "再撤销对源文件的删除。"
            ),
            structured_content={
                "operation": "move",
                "path": source.original_raw,
                "toPath": target.original_raw,
                "bytes": len(raw),
                "effect": receipt.as_dict(),
            },
        )

    def _ensure_checkpoint(self, call: ToolCall, binding: WorkspaceBinding) -> None:
        _ensure_checkpoint(self._checkpoint_coordinator, call, binding)
