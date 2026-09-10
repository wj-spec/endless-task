"""`WriteWorkspaceFileTool`（从 fs_tools.py 拆出，行为零改动）。"""

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
from pathlib import Path
from .common import _PATH_LOCKS, _backend_policy_and_trace, _log_effect, _now_iso, _read_text_or_none, _record_mutation, _record_undo_write, _sha256_bytes


class WriteWorkspaceFileTool:
    definition = ToolDefinition(
        name="write_workspace_file",
        description=(
            "在工作区根内写入或覆盖一个 UTF-8 文本文件。path 为相对工作区根的路径；"
            "会创建缺失的父目录。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "content": {"type": "string", "maxLength": 512_000},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=15.0,
        max_output_characters=4_000,
    )

    def __init__(
        self,
        resolver,
        effect_log: EffectLog,
        *,
        max_write_bytes: int = 512_000,
        checkpoint_coordinator=None,
        execution_backend=None,
        undo_service=None,
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._max_write_bytes = max_write_bytes
        # M3B run-level (方案 A): optional per-run workspace checkpoint
        # coordinator; the tool snapshots the workspace before its first
        # write so a failed run can be rolled back. None = current behavior.
        self._checkpoint_coordinator = checkpoint_coordinator
        # M3B enforcement (slice F): when set, mutations run through an
        # ExecutionEnvironment backend instead of direct host writes.
        self._execution_backend = execution_backend
        # A5 撤销：记录变更前内容，供用户一键回滚（None = 不记录）。
        self._undo_service = undo_service

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在写入工作区文件",
            completed="已写入工作区文件",
            failed="写入工作区文件失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        target = call.require_argument("path", str)
        return ToolApprovalPrompt(
            summary="允许写入工作区文件吗？",
            reason=(
                f"将写入/覆盖工作区文件：{target}\n"
                "只影响当前工作区目录；批准后立即执行。"
            ),
            metadata={"toolName": "write_workspace_file", "path": target},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        path = call.require_argument("path", str)
        content = call.require_argument("content", str)
        if len(content.encode("utf-8")) > self._max_write_bytes:
            raise ToolError(
                "write_too_large",
                f"内容超过写入上限（{self._max_write_bytes} 字节）。",
                retryable=False,
            )
        resolved = resolve_workspace_path(binding.root, path)
        if resolved.canonical.exists() and resolved.canonical.is_dir():
            raise ToolError(
                "path_is_directory",
                "路径指向目录，不能覆盖为文件。",
                retryable=False,
            )
        if self._execution_backend is not None:
            return await self._execute_backend_write(
                call, token, binding, path, content, resolved
            )
        self._ensure_checkpoint(call, binding)
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_content = _read_text_or_none(resolved.canonical)
            before_hash = (
                _sha256_bytes(resolved.canonical.read_bytes())
                if resolved.canonical.exists()
                else None
            )
            resolved.canonical.parent.mkdir(parents=True, exist_ok=True)
            resolved.canonical.write_text(content, encoding="utf-8")
        after_hash = sha256_text(content)
        _record_undo_write(
            self._undo_service,
            call,
            binding,
            resolved,
            before_content=before_content,
            after_hash=after_hash,
        )
        _record_mutation(
            self._checkpoint_coordinator,
            call,
            binding,
            resolved,
            operation="file_write",
            before_hash=before_hash,
            after_hash=after_hash,
        )
        receipt = EffectReceipt(
            kind="file_write",
            path=str(resolved.canonical),
            sha256=sha256_text(content),
            executed_at=_now_iso(),
        )
        _log_effect(self._effect_log, call, binding, "write_file", resolved.original_raw, receipt)
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"已写入工作区文件：{resolved.original_raw}（{len(content)} 字符）。",
            structured_content={
                "path": resolved.original_raw,
                "bytes": len(content.encode("utf-8")),
                "effect": receipt.as_dict(),
            },
        )


    async def _execute_backend_write(
        self,
        call: ToolCall,
        token: CancellationToken,
        binding: WorkspaceBinding,
        path: str,
        content: str,
        resolved,
    ) -> ToolResult:
        """Write through the enforcement backend (M3B slice F).

        Path containment is re-checked by the backend under its own policy;
        the run-level checkpoint + path lock semantics are preserved, the
        ledger row is appended by the backend (with the run id), and the
        workspace audit log + ToolResult shape stay identical to the direct
        path so consumers (task records, receipts) cannot tell the modes
        apart.
        """
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
                    effect_id=f"enforced_write:{relative}:{run_id}",
                    tool_call_id=call.id,
                    path=relative,
                    operation=Operation.WRITE,
                    policy=policy,
                    trace=trace,
                    requested_at=_now_iso(),
                    content=content,
                )
            )
        audit_receipt = EffectReceipt(
            kind="file_write",
            path=str(resolved.canonical),
            sha256=sha256_text(content),
            executed_at=_now_iso(),
        )
        _record_undo_write(
            self._undo_service,
            call,
            binding,
            resolved,
            before_content=before_content,
            after_hash=audit_receipt.sha256,
        )
        _log_effect(
            self._effect_log,
            call,
            binding,
            "write_file",
            resolved.original_raw,
            audit_receipt,
        )
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"已写入工作区文件：{resolved.original_raw}（{len(content)} 字符）。",
            structured_content={
                "path": resolved.original_raw,
                "bytes": len(content.encode("utf-8")),
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
            # Checkpoint is best-effort for observability/recovery; a
            # snapshot failure must not block the user's write.
            return
