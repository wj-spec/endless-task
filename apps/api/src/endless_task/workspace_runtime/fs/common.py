"""文件类工具的共享底座（从 fs_tools.py 拆出，行为零改动）。

包含哈希/解码、时间戳、checkpoint、行匹配与行差异、变更记录、撤销记录与
effect 日志，以及按路径的写锁 `_PathLockManager`。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from endless_task.tooling import ToolCall, ToolError

from ..effect_log import EffectLog, EffectReceipt
from ..resolver import WorkspaceBinding

#: 撤销记录失败只记日志、不影响写入结果（与拆分前的模块级 logger 同名同义）。
logger = logging.getLogger(__name__)


def _sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _decode_utf8(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(
            "binary_content",
            "文件不是 UTF-8 文本，拒绝读取/写入（工作区工具只处理文本文件）。",
            retryable=False,
        ) from error


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


#: 全工具共享的路径写锁表（拆分前是 fs_tools 模块级单例）。
_PATH_LOCKS = _PathLockManager()
