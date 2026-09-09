"""工作区文件系统工具（R5.12b）：读/写/列/删，锁定工作区根。

- 路径安全统一走 path_safety.resolve_workspace_path（canonicalize + containment）。
- 写路径 per-path 串行队列（参考 pi file-mutation-queue），防并发交错覆盖。
- 写/删成功落副作用日志并返回 EffectReceipt；日志失败标记 unknown_outcome。
- 删除恒显式确认（requires_explicit_confirmation 恒真）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
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

logger = logging.getLogger(__name__)

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
            "按名称读取 <available_skills> 里列出的技能正文（传 name）。"
            "该工具只读且仅允许访问技能目录。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "技能名（与目录中的 <name> 一致）。",
                },
                "locator": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "兼容写法：skill://<scope>/<name>。",
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 2048},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "anyOf": [
                {"required": ["name"]},
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
        name_resolver_provider: Optional[
            Callable[[str], Optional[Callable[[str], Optional[Path]]]]
        ] = None,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._skill_root_provider = skill_root_provider
        self._locator_resolver_provider = locator_resolver_provider
        self._name_resolver_provider = name_resolver_provider
        self._max_file_bytes = max_file_bytes
        # 未启用 locator 解析时，不要把 locator 写成"推荐"——否则模型会先试
        # 一次必然失败的 locator 调用（S1 live 实测发生过）。
        if locator_resolver_provider is None:
            schema = dict(type(self).definition.input_schema)
            properties = dict(schema["properties"])
            properties.pop("locator", None)
            self.definition = replace(
                type(self).definition,
                description=(
                    "按名称读取 <available_skills> 里列出的技能正文（传 name）；"
                    "该工具只读且仅允许访问技能目录。"
                ),
                input_schema={
                    **schema,
                    "properties": properties,
                    "anyOf": [
                        {"required": ["name"]},
                        {"required": ["path"]},
                    ],
                },
            )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在读取技能",
            completed="已读取技能",
            failed="读取技能失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        name = call.optional_argument("name", str, None)
        locator = call.optional_argument("locator", str, None)
        if name is not None:
            canonical = self._resolve_name(name, call)
        elif locator is not None:
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

    def _resolve_name(self, name: str, call: ToolCall) -> Path:
        """S3：按技能名解析（目录只暴露 name，不暴露路径）。"""
        if self._name_resolver_provider is None:
            raise ToolError(
                "name_unavailable",
                "技能名解析不可用；请改用 path。",
                retryable=False,
            )
        resolver = self._name_resolver_provider(call.conversation_id)
        if resolver is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{name}。",
                retryable=False,
            )
        try:
            canonical = resolver(name)
        except (ValueError, OSError) as error:
            raise ToolError(
                "invalid_skill_name", f"技能名无效：{name}。", retryable=False
            ) from error
        if canonical is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{name}（可能已禁用或依赖不满足）。",
                retryable=False,
            )
        return self._guard_containment(canonical, call)

    def _guard_containment(self, canonical: Path, call: ToolCall) -> Path:
        roots = self._skill_root_provider(call.conversation_id)
        resolved = resolve_external_read_path(roots, str(canonical))
        return resolved.canonical

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


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _ensure_checkpoint(coordinator, call: ToolCall, binding: WorkspaceBinding) -> None:
    """Run-level 工作区快照（best-effort，失败不阻塞写入）。"""
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


def _match_line_numbers(text: str, needle: str) -> list[int]:
    """needle 每次出现所在的行号（1 起）。"""
    numbers: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        numbers.append(text.count("\n", 0, index) + 1)
        start = index + max(1, len(needle))
    return numbers


def _line_diff(
    before: str,
    after: str,
    *,
    max_lines: int = 120,
    context: int = 2,
) -> list[str]:
    """紧凑行级 diff（公共前后缀裁剪 + LCS，过大时退化为整段增删）。"""
    left = before.split("\n")
    right = after.split("\n")
    start = 0
    while start < len(left) and start < len(right) and left[start] == right[start]:
        start += 1
    end_left = len(left)
    end_right = len(right)
    while (
        end_left > start
        and end_right > start
        and left[end_left - 1] == right[end_right - 1]
    ):
        end_left -= 1
        end_right -= 1
    head = left[:start]
    tail = left[end_left:]
    mid_left = left[start:end_left]
    mid_right = right[start:end_right]

    body: list[str] = []
    if len(mid_left) + len(mid_right) > 400:
        body = [f"- {line}" for line in mid_left] + [
            f"+ {line}" for line in mid_right
        ]
    else:
        rows = len(mid_left)
        cols = len(mid_right)
        table = [[0] * (cols + 1) for _ in range(rows + 1)]
        for i in range(rows - 1, -1, -1):
            for j in range(cols - 1, -1, -1):
                if mid_left[i] == mid_right[j]:
                    table[i][j] = table[i + 1][j + 1] + 1
                else:
                    table[i][j] = max(table[i + 1][j], table[i][j + 1])
        i = 0
        j = 0
        while i < rows and j < cols:
            if mid_left[i] == mid_right[j]:
                body.append(f"  {mid_left[i]}")
                i += 1
                j += 1
            elif table[i + 1][j] >= table[i][j + 1]:
                body.append(f"- {mid_left[i]}")
                i += 1
            else:
                body.append(f"+ {mid_right[j]}")
                j += 1
        while i < rows:
            body.append(f"- {mid_left[i]}")
            i += 1
        while j < cols:
            body.append(f"+ {mid_right[j]}")
            j += 1

    lines = (
        [f"  {line}" for line in head[-context:]]
        + body
        + [f"  {line}" for line in tail[:context]]
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["…（diff 已截断）"]
    return lines


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
        relative = resolved.canonical.relative_to(
            Path(binding.root).expanduser().resolve()
        ).as_posix()
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


def _backend_policy_and_trace(binding: WorkspaceBinding, run_id: str):
    """ExecutionPolicy + TraceContext for an enforcement backend call."""
    from endless_task.execution_env.protocol import (
        ExecutionPolicy,
        FileMutationOperation,
        FileMutationRequest,
    )
    from endless_task.runtime_ledger.protocol import TraceContext

    policy = ExecutionPolicy(
        workspace_root=str(Path(binding.root).resolve()),
        read_allow_paths=(),
        write_allow_paths=(),
    )
    trace = TraceContext(trace_id=run_id, run_id=run_id, correlation_id=run_id)
    return policy, trace, FileMutationRequest, FileMutationOperation


def _read_text_or_none(path) -> Optional[str]:
    """读取文本内容用于撤销；不存在/二进制/过大 → None（该次操作不可撤销）。"""
    try:
        if not path.exists() or not path.is_file():
            return None
        if path.stat().st_size > 512_000:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _undo_context(call: ToolCall, binding: WorkspaceBinding, resolved):
    """撤销日志需要的公共字段。"""
    return {
        "conversation_id": call.conversation_id,
        "target": resolved.original_raw,
        "workspace_root": str(binding.root),
        "workspace_id": getattr(binding, "workspace_id", None),
        "run_id": call.response_variant_id or call.id,
        "tool_execution_id": None,
    }


def _record_undo_write(
    service,
    call: ToolCall,
    binding: WorkspaceBinding,
    resolved,
    *,
    before_content: Optional[str],
    after_hash: Optional[str],
) -> None:
    if service is None:
        return
    try:
        service.record_file_write(
            before_exists=before_content is not None,
            before_content=before_content,
            after_hash=after_hash,
            **_undo_context(call, binding, resolved),
        )
    except Exception:  # noqa: BLE001 撤销记录失败不影响写入结果
        logger.debug("Undo write record failed", exc_info=True)


def _record_undo_delete(
    service,
    call: ToolCall,
    binding: WorkspaceBinding,
    resolved,
    *,
    before_content: Optional[str],
) -> None:
    if service is None or before_content is None:
        return
    try:
        service.record_file_delete(
            before_content=before_content,
            **_undo_context(call, binding, resolved),
        )
    except Exception:  # noqa: BLE001 撤销记录失败不影响删除结果
        logger.debug("Undo delete record failed", exc_info=True)


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
