"""工作区文件系统工具（R5.12b）：读/写/列/删，锁定工作区根。

- 路径安全统一走 path_safety.resolve_workspace_path（canonicalize + containment）。
- 写路径 per-path 串行队列（参考 pi file-mutation-queue），防并发交错覆盖。
- 写/删成功落副作用日志并返回 EffectReceipt；日志失败标记 unknown_outcome。
- 删除恒显式确认（requires_explicit_confirmation 恒真）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

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

from .effect_log import EffectLog, EffectReceipt, sha256_text


def _sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
from .path_safety import (
    resolve_external_read_path,
    resolve_read_path_with_variants,
    resolve_workspace_path,
)
from .resolver import WorkspaceBinding

MAX_LIST_ITEMS = 200
MAX_READ_LINES = 500


class _PathLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def acquire(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


_PATH_LOCKS = _PathLockManager()


def _decode_utf8(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(
            "binary_content",
            "文件不是 UTF-8 文本，拒绝读取/写入（工作区工具只处理文本文件）。",
            retryable=False,
        ) from error


class ReadSkillFileTool:
    definition = ToolDefinition(
        name="read_skill_file",
        description=(
            "读取系统提示词中列出的技能正文。可传 locator（skill://user/name，推荐）"
            "或 available_skills 提供的绝对路径（兼容）；该工具只读且仅允许访问技能目录。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "locator": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "技能 locator，形如 skill://user/<name>。",
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 2048},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "anyOf": [
                {"required": ["locator"]},
                {"required": ["path"]},
            ],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=40_000,
    )

    def __init__(
        self,
        skill_root_provider: Callable[[str], tuple[Path, ...]],
        *,
        locator_resolver_provider: Optional[
            Callable[[str], Optional[Callable[[str], Optional[Path]]]]
        ] = None,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._skill_root_provider = skill_root_provider
        self._locator_resolver_provider = locator_resolver_provider
        self._max_file_bytes = max_file_bytes

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在读取技能",
            completed="已读取技能",
            failed="读取技能失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        locator = call.optional_argument("locator", str, None)
        if locator is not None:
            canonical = self._resolve_locator(locator, call)
        else:
            roots = self._skill_root_provider(call.conversation_id)
            resolved = resolve_external_read_path(
                roots, call.require_argument("path", str)
            )
            canonical = resolved.canonical
        if not canonical.exists():
            raise ToolError("path_not_found", f"技能文件不存在。", retryable=False)
        if not canonical.is_file():
            raise ToolError("path_is_directory", "路径指向目录。", retryable=False)
        raw = canonical.read_bytes()
        if len(raw) > self._max_file_bytes:
            raise ToolError(
                "file_too_large",
                f"技能文件超过读取上限（{self._max_file_bytes} 字节）。",
                retryable=False,
            )
        text = _decode_utf8(raw)
        lines = text.splitlines() or [""]
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
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"[来源：技能 {canonical}:L{start_line}-L{end_line}]\n"
                f"{selected}"
            ),
            structured_content={
                "path": str(canonical),
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": len(lines),
            },
        )

    def _resolve_locator(self, locator: str, call: ToolCall) -> Path:
        if self._locator_resolver_provider is None:
            raise ToolError(
                "locator_unavailable",
                "技能 locator 解析未启用；请改用 path。",
                retryable=False,
            )
        resolver = self._locator_resolver_provider(call.conversation_id)
        if resolver is None:
            raise ToolError(
                "locator_unavailable",
                "技能 locator 解析未启用；请改用 path。",
                retryable=False,
            )
        try:
            canonical = resolver(locator)
        except (ValueError, OSError) as error:
            raise ToolError(
                "invalid_skill_locator",
                f"技能 locator 无效：{locator}。",
                retryable=False,
            ) from error
        if canonical is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{locator}。",
                retryable=False,
            )
        # Locator resolution must still satisfy root containment: re-run the
        # canonical path through the same resolver used for legacy paths and
        # return its canonical (symlink-resolved) form.
        roots = self._skill_root_provider(call.conversation_id)
        return resolve_external_read_path(roots, str(canonical)).canonical


class ReadWorkspaceFileTool:
    definition = ToolDefinition(
        name="read_workspace_file",
        description=(
            "读取当前工作区内文件的 UTF-8 文本内容。path 为相对工作区根的路径；"
            "可按行分段读取；回答时必须引用结果提供的来源标签。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=40_000,
    )

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
        raw = resolved.canonical.read_bytes()
        if len(raw) > self._max_file_bytes:
            raise ToolError(
                "file_too_large",
                f"文件超过读取上限（{self._max_file_bytes} 字节）。",
                retryable=False,
            )
        text = _decode_utf8(raw)
        lines = text.splitlines() or [""]
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
        label = (
            f"工作区文件 {resolved.original_raw}:L{start_line}-L{end_line}"
            + (f"（实际路径 {resolved.canonical}，macOS 变体 {resolved.variant}）"
               if resolved.variant != "exact" else "")
        )
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"[来源：{label}]\n{selected}",
            structured_content={
                "path": resolved.original_raw,
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": len(lines),
                "variant": resolved.variant,
            },
        )


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
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._max_write_bytes = max_write_bytes
        # M3B run-level (方案 A): optional per-run workspace checkpoint
        # coordinator; the tool snapshots the workspace before its first
        # write so a failed run can be rolled back. None = current behavior.
        self._checkpoint_coordinator = checkpoint_coordinator

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
        self._ensure_checkpoint(call, binding)
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_hash = (
                _sha256_bytes(resolved.canonical.read_bytes())
                if resolved.canonical.exists()
                else None
            )
            resolved.canonical.parent.mkdir(parents=True, exist_ok=True)
            resolved.canonical.write_text(content, encoding="utf-8")
        after_hash = sha256_text(content)
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
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._checkpoint_coordinator = checkpoint_coordinator

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
        self._ensure_checkpoint(call, binding)
        lock = await _PATH_LOCKS.acquire(str(resolved.canonical))
        async with lock:
            before_hash = _sha256_bytes(resolved.canonical.read_bytes())
            resolved.canonical.unlink()
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


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _record_mutation(
    coordinator,
    call: ToolCall,
    binding: WorkspaceBinding,
    resolved,
    *,
    operation: str,
    before_hash: Optional[str],
    after_hash: Optional[str],
) -> None:
    """Ledger an agent file side effect for run-level restore (M3B A).

    Best-effort like the checkpoint hook: failures only degrade restore
    attribution to "user-owned", never block the tool result.
    """
    if coordinator is None:
        return
    try:
        relative = resolved.canonical.relative_to(binding.root).as_posix()
    except (ValueError, OSError):
        return
    run_id = call.response_variant_id or call.id
    try:
        coordinator.record_effect(
            run_id=run_id,
            effect_id=f"{operation}:{relative}:{run_id}:{(after_hash or before_hash or 'x')[:16]}",
            path=relative,
            operation=operation,
            before_hash=before_hash,
            after_hash=after_hash,
        )
    except Exception:
        return


def _log_effect(
    log: EffectLog,
    call: ToolCall,
    binding: WorkspaceBinding,
    operation: str,
    detail: str,
    receipt: EffectReceipt,
) -> None:
    try:
        log.append(
            conversation_id=call.conversation_id,
            workspace_id=binding.workspace_id,
            workspace_root=str(binding.root),
            operation=operation,
            detail=detail,
            receipt=receipt,
        )
    except ToolError:
        raise ToolError(
            "unknown_outcome",
            "操作已完成，但本地审计日志写入失败；请到工作区设置检查日志目录后核对结果。",
            retryable=False,
        )
