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

#: 撤销记录失败只记日志、不影响写入结果。
#: 日志名沿用拆分前的 `endless_task.workspace_runtime.fs_tools`：日志过滤/告警按名字
#: 配置，用 `__name__` 会静默变成 `...workspace_runtime.fs.common`。
logger = logging.getLogger("endless_task.workspace_runtime.fs_tools")


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


def _undo_text_or_none(raw: bytes) -> Optional[str]:
    """把原始字节折成「可撤销文本」；过大/非 UTF-8 → None（该次操作不可撤销）。

    口径与旧的按路径前置读**逐字一致**：>512_000 字节不记、非 UTF-8 不记、
    换行按 `Path.read_text` 的 universal newlines 归一（`\\r\\n`/`\\r` → `\\n`）。
    """
    if len(raw) > 512_000:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


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


# ---- 内核（Rust）读写删：闭合 TOCTOU（RK-3） ----
# 三个 fs 工具都需要「按句柄而不是按路径」操作：逐段 openat + O_NOFOLLOW，
# 任一段是 symlink 即拒；拿到的是句柄，校验之后换不掉。
from endless_task.tooling import ToolError as _KernelToolError

from ..._native.fs import (
    NativeFsError as _NativeFsError,
    list_beneath as _native_list,
    mkdir_beneath as _native_mkdir,
    open_beneath as _native_open_text,
    read_beneath as _native_read,
    remove_beneath as _native_remove,
    write_beneath as _native_write,
)


def _kernel_workspace_relative(binding, resolved):
    """内核要的 (工作区根, 工作区相对路径)。"""
    root = binding.root.resolve()
    return root, resolved.canonical.relative_to(root).as_posix()


def _kernel_error(error, resolved):
    """把内核错误映射成既有 ToolError 词汇（**指名**，不静默降级）。"""
    if error.code == "path_not_found":
        return _KernelToolError(
            "path_not_found",
            f"文件不存在：{resolved.original_raw}。",
            retryable=False,
        )
    # 其余（symlink 段 / 非目录 / 变化中）统一按「校验后发生变化」拒绝，
    # 文案里指名底层原因，便于诊断。
    return _KernelToolError(
        "path_escape",
        f"文件在校验后发生变化，已拒绝（{error.code}）。",
        retryable=False,
    )


def kernel_read(binding, resolved, max_bytes):
    root, rel = _kernel_workspace_relative(binding, resolved)
    try:
        return _native_read(root, rel, int(max_bytes))
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_read_undo_snapshot(binding, resolved):
    """undo/diff 用的「前置读」快照：一次句柄读，返回 `(可撤销文本, 原始字节)`。

    取代四个 fs 工具里按路径的前置读（RK-3 C6）：按路径读会**跟随 symlink** ——
    校验通过后把中间段换成指向区外的链接，就能把区外内容读进撤销日志/哈希，
    且全程不需要竞态。现在与写/删走同一条句柄链（逐段 `openat` + `O_NOFOLLOW`）。

    语义与旧前置读在稳定状态下逐字一致：
    - 文件不存在 → `(None, None)`（新建文件没有 before）；
    - 存在但过大/非 UTF-8 → `(None, raw)`（不可撤销，但 `before_hash` 仍按原始字节）；
    - 正常文本 → `(text, raw)`，换行按 universal newlines 归一。

    **有意差异（就是要闭的缝）**：旧实现把一切 OSError 软化成「不可撤销」，
    那会把 symlink 攻击也一并软化掉（照旧把区外内容读回来）。现在只有
    「文件不存在」软化；路径在校验后被换成 symlink / 目录 / 不可读一律**指名拒绝**。
    """
    try:
        raw = kernel_read(binding, resolved, 1 << 62)
    except _KernelToolError as error:
        if error.code == "path_not_found":
            return None, None
        raise
    return _undo_text_or_none(raw), raw


def kernel_write(binding, resolved, data):
    root, rel = _kernel_workspace_relative(binding, resolved)
    try:
        _native_write(root, rel, bytes(data))
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_remove(binding, resolved):
    root, rel = _kernel_workspace_relative(binding, resolved)
    try:
        _native_remove(root, rel)
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_open_text(workspace_root, relative_path, resolved):
    """按句柄打开文本文件（逐段 nofollow，返回文件对象）；失败直接抛 ToolError。

    专门给「流式按行读」用：保持不整读大文件的语义，但把路径换成句柄。
    """
    try:
        return _native_open_text(workspace_root, relative_path)
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_mkdir_parent(binding, resolved):
    """确保目标文件的**父目录链**存在（等价 parent.mkdir(parents=True, exist_ok=True)）。

    路径不是交给内核去 mkdir：内核逐段 `openat` + `O_NOFOLLOW` 后再 `mkdirat`，
    所以中途被换成 symlink 时**不会在区外建目录**。
    """
    root = binding.root.resolve()
    parent = resolved.canonical.parent
    if parent == root:
        return
    rel = parent.relative_to(root).as_posix()
    try:
        _native_mkdir(root, rel)
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_list(binding, resolved):
    """列目录（通过句柄）。返回 [(name, is_dir, is_file, is_symlink, size), ...]。

    `is_dir`/`is_file`/`size` 是 **lstat 语义**（不跟随符号链接）——
    跟随正是要闭合的那条缝（会读出区外目标的大小）。
    """
    root, rel = _kernel_workspace_relative(binding, resolved)
    if rel == ".":
        rel = ""  # 工作区根本身
    try:
        return _native_list(root, rel)
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def kernel_mkdir(binding, resolved):
    """创建工作区内的**目录本身**（含父链）——逐段 nofollow。

    与 kernel_mkdir_parent 的区别：那个建的是"目标文件的父目录"，
    这个建的就是 resolved 指向的那个目录。
    """
    root, rel = _kernel_workspace_relative(binding, resolved)
    if rel == ".":
        return  # 工作区根本身，已存在
    try:
        _native_mkdir(root, rel)
    except _NativeFsError as error:
        raise _kernel_error(error, resolved) from error


def external_relative(roots, canonical):
    """在多个只读根里找出 canonical 属于哪一个，返回 (根, 相对路径)；都不匹配返回 None。

    用 `resolve(strict=False)` 归一化根（与 path_safety.resolve_external_read_path 同口径），
    否则 macOS 上 `/var` 与 `/private/var` 的差异会让 relative_to 误判。
    """
    from pathlib import Path

    for root in roots:
        try:
            normalized = Path(root).expanduser().resolve(strict=False)
        except OSError:
            continue
        try:
            relative = Path(canonical).relative_to(normalized)
        except ValueError:
            continue
        return normalized, relative.as_posix()
    return None


def kernel_read_external(roots, canonical, max_bytes, label):
    """从**只读根**之一下按句柄读文件（逐段 nofollow）。

    与 kernel_read 的区别：源不是单一工作区根，而是根列表；先定位属于哪个根。
    定位不到 ⇒ path_escape（**不静默换成按路径读**）。
    """
    located = external_relative(roots, canonical)
    if located is None:
        raise _KernelToolError(
            "path_escape",
            f"{label}越出允许的只读目录，已拒绝。",
            retryable=False,
        )
    root, rel = located
    try:
        return _native_read(root, rel, int(max_bytes))
    except _NativeFsError as error:
        if error.code == "file_too_large":
            raise _KernelToolError(
                "file_too_large",
                f"{label}超过读取上限（{max_bytes} 字节）。",
                retryable=False,
            ) from error
        raise _KernelToolError(
            "path_escape",
            f"{label}在校验后发生变化，已拒绝读取（{error.code}）。",
            retryable=False,
        ) from error
