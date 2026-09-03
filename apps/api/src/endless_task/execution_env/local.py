"""Local execution backend (M3B AP-301).

Implements the read / mutate / process methods of the frozen
``ExecutionEnvironment`` contract against a real workspace directory,
contained by ``ExecutionPolicy``:

- path handling replicates the legacy workspace lexical rules exactly
  (relative paths only, no ``..``, canonical containment inside the root;
  error codes ``invalid_path`` / ``path_escape`` match the legacy tools),
- every write/delete/process call produces a protocol ``EffectReceipt``;
  when the optional receipt sink fails the receipt reports outcome
  ``unknown`` (never "not executed"),
- process output is bounded and truncated with a flag; an unknown exit code
  (timeout/kill) is reported as ``unknown``, never fabricated.

Checkpoint/restore are not part of this slice (AP-305) and fail closed with
a structured error instead of silently no-oping.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from endless_task.agent_platform import (
    AgentPlatformError,
    EffectOutcome,
    EffectReceipt,
)
from endless_task.runtime_ledger import TraceContext

from .checkpoint import (
    CheckpointManifest,
    apply_restore,
    create_checkpoint,
    plan_restore,
    read_manifest,
)
from .ledger import FileMutationLedger, LedgerEntry, ledger_timestamp
from .protocol import (
    CheckpointRef,
    CheckpointRequest,
    ExecutionEnvironment,
    ExecutionPolicy,
    FileMutationOperation,
    FileMutationRequest,
    FileReadResult,
    ProcessRequest,
    ProcessResult,
    ReadFileRequest,
    RestoreRequest,
    RestoreResult,
)

_MARKER = "\n…[输出超长已截断]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _sha256_text(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


class LocalExecutionBackend(ExecutionEnvironment):
    """Unrestricted-within-workspace local backend."""

    def __init__(
        self,
        *,
        receipt_sink: Optional[Callable[[EffectReceipt], None]] = None,
        checkpoint_store: Optional[Path] = None,
        ledger: Optional[FileMutationLedger] = None,
    ) -> None:
        if receipt_sink is not None and not callable(receipt_sink):
            raise AgentPlatformError(
                "invalid_execution_value",
                "receipt_sink must be callable",
            )
        self._receipt_sink = receipt_sink
        if checkpoint_store is not None:
            self._store = Path(checkpoint_store).expanduser().resolve(strict=False)
        else:
            self._store = None
        self._ledger = ledger

    async def read_file(self, request: ReadFileRequest) -> FileReadResult:
        if not isinstance(request, ReadFileRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Read requires a ReadFileRequest",
            )
        root, target = _resolve_path(
            request.path,
            request.policy,
            allow_paths=request.policy.read_allow_paths,
        )
        try:
            if not target.exists():
                raise AgentPlatformError(
                    "path_not_found",
                    f"文件不存在：{request.path}。",
                )
            if target.is_dir():
                raise AgentPlatformError(
                    "path_is_directory",
                    "路径指向目录。",
                )
            raw = target.read_bytes()
        except AgentPlatformError:
            raise
        except OSError as error:
            raise AgentPlatformError(
                "file_read_failed",
                "文件读取失败。",
                details={"error_type": type(error).__name__},
            ) from error
        try:
            text = raw.decode(request.encoding)
        except (LookupError, UnicodeDecodeError) as error:
            raise AgentPlatformError(
                "binary_content",
                "文件不是 UTF-8 文本，拒绝读取。",
                retryable=False,
            ) from error
        limit = request.policy.max_output_characters
        truncated = len(text) > limit
        if truncated:
            text = text[:limit] + _MARKER
        return FileReadResult(
            path=request.path,
            content=text,
            content_hash=_sha256_text(text),
            truncated=truncated,
        )

    async def mutate_file(
        self,
        request: FileMutationRequest,
    ) -> EffectReceipt:
        if not isinstance(request, FileMutationRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Mutation requires a FileMutationRequest",
            )
        root, target = _resolve_path(
            request.path,
            request.policy,
            allow_paths=request.policy.write_allow_paths,
        )
        before_hash: Optional[str] = None
        if target.exists():
            before_hash = _sha256_bytes(target.read_bytes())
        if (
            request.expected_before_hash is not None
            and before_hash != request.expected_before_hash
        ):
            raise AgentPlatformError(
                "mutation_conflict",
                "文件在调用前已被修改，拒绝写入。",
                retryable=False,
                details={
                    "expected": request.expected_before_hash,
                    "actual": before_hash,
                },
            )
        if request.operation is FileMutationOperation.WRITE:
            if target.exists() and target.is_dir():
                raise AgentPlatformError(
                    "path_is_directory",
                    "路径指向目录，不能覆盖为文件。",
                )
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(request.content or "", encoding="utf-8")
            except OSError as error:
                raise AgentPlatformError(
                    "file_write_failed",
                    "文件写入失败。",
                    details={"error_type": type(error).__name__},
                ) from error
            effect_type = "file_write"
            summary = "已写入本地工作区文件。"
            after_ref = _sha256_text(request.content or "")
        elif request.operation is FileMutationOperation.DELETE:
            if not target.exists():
                raise AgentPlatformError(
                    "path_not_found",
                    f"文件不存在：{request.path}。",
                )
            if target.is_dir():
                raise AgentPlatformError(
                    "path_is_directory",
                    "目录删除不开放给本地后端。",
                )
            try:
                target.unlink()
            except OSError as error:
                raise AgentPlatformError(
                    "file_delete_failed",
                    "文件删除失败。",
                    details={"error_type": type(error).__name__},
                ) from error
            effect_type = "file_delete"
            summary = "已删除本地工作区文件。"
            after_ref = None
        else:  # pragma: no cover - enum is exhaustive
            raise AgentPlatformError(
                "invalid_execution_value",
                "Unsupported file mutation operation",
            )
        receipt = self._issue_receipt(
            effect_id=request.effect_id,
            tool_call_id=request.tool_call_id,
            effect_type=effect_type,
            target=str(target),
            started_at=request.requested_at,
            committed_outcome=True,
            safe_summary=summary,
            before_ref=before_hash,
            after_ref=after_ref,
            idempotency_key=request.idempotency_key,
        )
        if self._ledger is not None:
            try:
                relative = target.relative_to(root).as_posix()
            except ValueError:
                relative = request.path
            self._ledger.append(
                LedgerEntry(
                    effect_id=request.effect_id,
                    path=relative,
                    operation=request.operation.value,
                    timestamp=ledger_timestamp(),
                    before_hash=before_hash,
                    after_hash=after_ref,
                )
            )
        return receipt

    async def run_process(self, request: ProcessRequest) -> ProcessResult:
        if not isinstance(request, ProcessRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Process requires a ProcessRequest",
            )
        root = _root_path(request.policy)
        cwd = _canonical_cwd(request.cwd, root)
        environment = _process_environment(request)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=str(cwd),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            raise AgentPlatformError(
                "process_spawn_failed",
                "进程启动失败。",
                retryable=False,
                details={"error_type": type(error).__name__},
            ) from error
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=request.policy.timeout_seconds,
            )
            exit_code = process.returncode
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except (ProcessLookupError, asyncio.CancelledError):
                pass
            stdout_bytes, stderr_bytes = b"", b""
            exit_code = None
        stdout = _decode_output(
            stdout_bytes,
            request.policy.max_output_characters,
        )
        stderr = _decode_output(
            stderr_bytes,
            request.policy.max_output_characters,
        )
        truncated = stdout.truncated or stderr.truncated
        if exit_code is None:
            outcome_text = (
                "进程执行超时或状态未知，结果需人工核对。"
            )
            committed = False
        else:
            outcome_text = f"进程已执行，退出码 {exit_code}。"
            committed = True
        receipt = self._issue_receipt(
            effect_id=request.effect_id,
            tool_call_id=request.tool_call_id,
            effect_type="process",
            target=request.cwd,
            started_at=request.requested_at,
            committed_outcome=committed,
            safe_summary=outcome_text,
            idempotency_key=request.idempotency_key,
        )
        return ProcessResult(
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            receipt=receipt,
            truncated=truncated,
        )

    async def checkpoint(self, request: CheckpointRequest) -> CheckpointRef:
        if not isinstance(request, CheckpointRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Checkpoint requires a CheckpointRequest",
            )
        if self._store is None:
            raise AgentPlatformError(
                "not_implemented_in_slice",
                "Checkpoint store 未配置，无法创建 checkpoint。",
                retryable=False,
            )
        root = _root_path(request.policy)
        manifest = create_checkpoint(
            root,
            self._store,
            checkpoint_id=request.run_id,
        )
        return CheckpointRef(
            checkpoint_id=manifest.checkpoint_id,
            run_id=request.run_id,
            backend="local-content",
            root_fingerprint=manifest.root_fingerprint,
            manifest_ref=f"manifests/{manifest.checkpoint_id}.json",
            created_at=request.created_at,
        )

    async def restore(self, request: RestoreRequest) -> RestoreResult:
        if not isinstance(request, RestoreRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Restore requires a RestoreRequest",
            )
        if self._store is None:
            raise AgentPlatformError(
                "not_implemented_in_slice",
                "Checkpoint store 未配置，无法恢复。",
                retryable=False,
            )
        if request.checkpoint.backend != "local-content":
            raise AgentPlatformError(
                "restore_backend_mismatch",
                "Checkpoint 不属于本地内容寻址后端。",
                retryable=False,
            )
        manifest = read_manifest(self._store, request.checkpoint.checkpoint_id)
        root = _root_path(request.policy)
        if request.dry_run:
            plan = plan_restore(manifest, root, ledger=self._ledger)
            return RestoreResult(
                applied=False,
                restored_paths=plan.would_apply,
                skipped_paths=(
                    plan.user_modified_skipped + plan.new_files_untouched
                ),
            )
        restored, skipped = apply_restore(
            manifest,
            root,
            self._store,
            ledger=self._ledger,
        )
        return RestoreResult(applied=True, restored_paths=restored, skipped_paths=skipped)

    def _issue_receipt(
        self,
        *,
        effect_id: str,
        tool_call_id: str,
        effect_type: str,
        target: str,
        started_at: str,
        committed_outcome: bool,
        safe_summary: str,
        before_ref: Optional[str] = None,
        after_ref: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> EffectReceipt:
        committed_at = _now_iso()
        outcome = EffectOutcome.COMMITTED if committed_outcome else EffectOutcome.UNKNOWN
        if not committed_outcome:
            committed_at = None
        receipt = EffectReceipt(
            effect_id=effect_id,
            tool_call_id=tool_call_id,
            effect_type=effect_type,
            target=target,
            started_at=started_at,
            outcome=outcome,
            backend="local",
            safe_summary=safe_summary,
            committed_at=committed_at,
            before_ref=before_ref,
            after_ref=after_ref,
            idempotency_key=idempotency_key,
        )
        if self._receipt_sink is not None:
            try:
                self._receipt_sink(receipt)
            except Exception as error:
                return EffectReceipt(
                    effect_id=effect_id,
                    tool_call_id=tool_call_id,
                    effect_type=effect_type,
                    target=target,
                    started_at=started_at,
                    outcome=EffectOutcome.UNKNOWN,
                    backend="local",
                    safe_summary=(
                        "副作用已发生，但本地审计日志写入失败，结果需人工核对。"
                    ),
                    committed_at=None,
                    before_ref=before_ref,
                    after_ref=after_ref,
                    idempotency_key=idempotency_key,
                )
        return receipt


def _root_path(policy: ExecutionPolicy) -> Path:
    root = Path(policy.workspace_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise AgentPlatformError(
            "invalid_execution_value",
            "workspace_root 不存在或不可用。",
            retryable=False,
        )
    return root


def _resolve_path(
    raw_path: str,
    policy: ExecutionPolicy,
    *,
    allow_paths: tuple[str, ...],
) -> tuple[Path, Path]:
    root = _root_path(policy)
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AgentPlatformError("invalid_path", "路径不能为空。")
    normalized = raw_path.strip()
    if normalized.startswith("/") or normalized.startswith("\\"):
        raise AgentPlatformError(
            "path_escape",
            "只接受相对工作区的路径，不接受绝对路径。",
        )
    parts = [
        part
        for part in normalized.replace("\\", "/").split("/")
        if part not in ("", ".")
    ]
    if any(part == ".." for part in parts):
        raise AgentPlatformError(
            "path_escape",
            "路径不允许包含 .. 片段。",
        )
    candidate = root
    for part in parts:
        candidate = candidate / part
    canonical = candidate.resolve(strict=False)
    try:
        canonical.relative_to(root)
    except ValueError as error:
        raise AgentPlatformError(
            "path_escape",
            "路径越出工作区根目录，已拒绝。",
        ) from error
    if allow_paths:
        allowed = False
        for allow in allow_paths:
            try:
                canonical.relative_to(_root_path(policy) / allow)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise AgentPlatformError(
                "path_not_allowed",
                "路径不在本次执行允许范围内。",
            )
    return root, canonical


def _canonical_cwd(raw_cwd: str, root: Path) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        raise AgentPlatformError("invalid_execution_value", "cwd 不能为空。")
    raw = Path(raw_cwd).expanduser()
    # Relative cwd is interpreted against the workspace root (matches the
    # legacy shell tool which runs inside binding.root).
    path = raw if raw.is_absolute() else root / raw
    cwd = path.resolve(strict=False)
    try:
        cwd.relative_to(root)
    except ValueError as error:
        raise AgentPlatformError(
            "path_escape",
            "进程 cwd 越出工作区根目录，已拒绝。",
        ) from error
    if not cwd.is_dir():
        raise AgentPlatformError(
            "invalid_execution_value",
            "进程 cwd 不存在。",
        )
    return cwd


def _process_environment(request: ProcessRequest) -> Optional[dict[str, str]]:
    allowlist = request.policy.environment_allowlist
    if not allowlist:
        return None
    environment: dict[str, str] = {}
    for key in allowlist:
        value = os.environ.get(key)
        if value is not None:
            environment[key] = value
    for key, value in request.environment.items():
        if key in allowlist:
            environment[key] = value
    return environment or None


@dataclass(frozen=True)
class _Decoded:
    text: str
    truncated: bool


def _decode_output(data: bytes, limit: int) -> _Decoded:
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + _MARKER
    return _Decoded(text=text, truncated=truncated)


__all__ = ["LocalExecutionBackend"]
