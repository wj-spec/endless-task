"""`DeleteWorkspaceFileTool`（从 fs_tools.py 拆出，行为零改动）。"""

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
from pathlib import Path
from .common import _PATH_LOCKS, _backend_policy_and_trace, _log_effect, _now_iso, _read_text_or_none, _record_mutation, _record_undo_delete, _sha256_bytes


class DeleteWorkspaceFileTool:
    definition = ToolDefinition(
        name="delete_workspace_file",
        description=(
            "删除工作区根内的一个文件。此操作不可恢复，始终需要用户明确确认；"
            "如需删除目录请说明后由用户手动处理。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=10.0,
        max_output_characters=2_000,
    )

    def __init__(
        self,
        resolver,
        effect_log: EffectLog,
        *,
        checkpoint_coordinator=None,
        execution_backend=None,
        undo_service=None,
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._checkpoint_coordinator = checkpoint_coordinator
        self._execution_backend = execution_backend
        # A5 撤销：删除前内容入撤销日志，支持"恢复刚删掉的文件"。
        self._undo_service = undo_service

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在删除工作区文件",
            completed="已删除工作区文件",
            failed="删除工作区文件失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return True

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        target = call.require_argument("path", str)
        return ToolApprovalPrompt(
            summary="允许执行 delete_workspace_file 吗？（不可恢复）",
            reason=(
                f"将永久删除工作区文件：{target}\n"
                "删除不可恢复，即使已开启提权模式也需要你逐次确认。"
            ),
            metadata={"toolName": "delete_workspace_file", "path": target},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        resolved = resolve_workspace_path(
            binding.root,
            call.require_argument("path", str),
        )
        if not resolved.canonical.exists():
            raise ToolError(
                "path_not_found",
                f"文件不存在：{resolved.original_raw}。",
                retryable=False,
            )
        if resolved.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "目录删除不开放给工具，请由用户手动处理。",
                retryable=False,
            )
        if self._execution_backend is not None:
            return await self._execute_backend_delete(
                call, token, binding, resolved
            )
        self._ensure_checkpoint(call, binding)
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_content = _read_text_or_none(resolved.canonical)
            before_hash = _sha256_bytes(resolved.canonical.read_bytes())
            resolved.canonical.unlink()
        _record_undo_delete(
            self._undo_service,
            call,
            binding,
            resolved,
            before_content=before_content,
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            resolved,
            operation="file_delete",
            before_hash=before_hash,
            after_hash=None,
        )
        receipt = EffectReceipt(
            kind="file_delete",
            path=str(resolved.canonical),
            executed_at=_now_iso(),
        )
        _log_effect(self._effect_log, call, binding, "delete_file", resolved.original_raw, receipt)
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"已删除工作区文件：{resolved.original_raw}。",
            structured_content={
                "path": resolved.original_raw,
                "effect": receipt.as_dict(),
            },
        )


    async def _execute_backend_delete(
        self,
        call: ToolCall,
        token: CancellationToken,
        binding: WorkspaceBinding,
        resolved,
    ) -> ToolResult:
        """Delete through the enforcement backend (M3B slice F)."""
        run_id = call.response_variant_id or call.id
        self._ensure_checkpoint(call, binding)
        relative = resolved.canonical.relative_to(
            Path(binding.root).expanduser().resolve()
        ).as_posix()
        policy, trace, Request, Operation = _backend_policy_and_trace(
            binding, run_id
        )
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_content = _read_text_or_none(resolved.canonical)
            receipt = await self._execution_backend.mutate_file(
                Request(
                    effect_id=f"enforced_delete:{relative}:{run_id}",
                    tool_call_id=call.id,
                    path=relative,
                    operation=Operation.DELETE,
                    policy=policy,
                    trace=trace,
                    requested_at=_now_iso(),
                )
            )
        audit_receipt = EffectReceipt(
            kind="file_delete",
            path=str(resolved.canonical),
            executed_at=_now_iso(),
        )
        _record_undo_delete(
            self._undo_service,
            call,
            binding,
            resolved,
            before_content=before_content,
        )
        _log_effect(
            self._effect_log,
            call,
            binding,
            "delete_file",
            resolved.original_raw,
            audit_receipt,
        )
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"已删除工作区文件：{resolved.original_raw}。",
            structured_content={
                "path": resolved.original_raw,
                "effect": audit_receipt.as_dict(),
            },
        )

    def _ensure_checkpoint(self, call: ToolCall, binding) -> None:
        coordinator = self._checkpoint_coordinator
        if coordinator is None:
            return
        run_id = call.response_variant_id or call.id
        try:
            coordinator.ensure_checkpoint(
                run_id=run_id,
                workspace_root=str(binding.root),
            )
        except Exception:
            return
