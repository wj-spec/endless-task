"""macOS Seatbelt sandbox backend (M3B AP-306).

Wraps ``run_process`` execution in a macOS ``sandbox-exec`` Seatbelt profile
derived from the ``ExecutionPolicy``:

- default deny, allow process execution and system file reads,
- file writes only inside the workspace root (plus any extra write allow
  paths), so mutations outside the workspace fail closed at the OS level,
- network is denied unless the policy says ``ALLOW_ALL`` (``ALLOW_HOSTS``
  cannot be expressed in a plain Seatbelt profile; it is denied here and
  must be enforced by an upper layer — never silently opened up),
- when ``sandbox-exec`` is unavailable or refuses to apply (e.g. restricted
  macOS), execution fails closed with ``sandbox_unavailable`` — there is no
  silent fallback to the unrestricted backend.

File read/mutate stay on the (policy-confined) local backend; the sandbox
layer is what confines process side effects.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from endless_task.agent_platform import (
    AgentPlatformError,
    EffectOutcome,
    EffectReceipt,
)

from .local import LocalExecutionBackend, _Decoded, _decode_output
from .protocol import (
    CheckpointRef,
    CheckpointRequest,
    ExecutionEnvironment,
    FileMutationRequest,
    FileReadResult,
    NetworkMode,
    ProcessRequest,
    ProcessResult,
    ReadFileRequest,
    RestoreRequest,
    RestoreResult,
)


def build_seatbelt_profile(
    *,
    workspace_root: str,
    write_allow_paths: tuple[str, ...] = (),
    network_mode: NetworkMode = NetworkMode.DENY,
) -> str:
    """Deterministic Seatbelt profile for one execution policy."""
    if not isinstance(network_mode, NetworkMode):
        raise AgentPlatformError(
            "invalid_execution_value",
            "network_mode must use a protocol enum value",
        )
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow file-read*)",
        "(allow sysctl-read)",
        f'(allow file-write* (subpath "{workspace_root}"))',
    ]
    for allow in write_allow_paths:
        lines.append(f'(allow file-write* (subpath "{allow}"))')
    if network_mode is NetworkMode.ALLOW_ALL:
        lines.append("(allow network*)")
    else:
        # DENY and ALLOW_HOSTS: a plain Seatbelt profile has no host allow
        # list, so outbound network is denied here (never opened silently).
        lines.append("(deny network*)")
    return "\n".join(lines)


def probe_seatbelt(sandbox_exec: str) -> bool:
    """Whether ``sandbox-exec`` can actually apply a sandbox here."""
    import subprocess

    try:
        result = subprocess.run(
            [sandbox_exec, "-p", "(allow default)", "/bin/echo", "probe"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class SeatbeltBackend(ExecutionEnvironment):
    """Local backend whose process execution is confined by Seatbelt."""

    def __init__(
        self,
        *,
        receipt_sink: Optional[object] = None,
        checkpoint_store: Optional[Path] = None,
        ledger: Optional[object] = None,
        sandbox_exec: Optional[str] = None,
    ) -> None:
        self._local = LocalExecutionBackend(
            receipt_sink=receipt_sink,
            checkpoint_store=checkpoint_store,
            ledger=ledger,
        )
        self._sandbox_exec = (
            sandbox_exec if sandbox_exec is not None else shutil.which("sandbox-exec")
        )

    async def read_file(self, request: ReadFileRequest) -> FileReadResult:
        return await self._local.read_file(request)

    async def mutate_file(self, request: FileMutationRequest) -> EffectReceipt:
        return await self._local.mutate_file(request)

    async def run_process(self, request: ProcessRequest) -> ProcessResult:
        if not isinstance(request, ProcessRequest):
            raise AgentPlatformError(
                "invalid_execution_value",
                "Process requires a ProcessRequest",
            )
        if not self._sandbox_exec:
            raise AgentPlatformError(
                "sandbox_unavailable",
                "sandbox-exec 不可用，拒绝在非受限环境执行。",
                retryable=False,
            )
        profile = build_seatbelt_profile(
            workspace_root=request.policy.workspace_root,
            write_allow_paths=request.policy.write_allow_paths,
            network_mode=request.policy.network_mode,
        )
        argv = (self._sandbox_exec, "-p", profile, *request.argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=request.cwd,
                env=_sandbox_environment(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            raise AgentPlatformError(
                "process_spawn_failed",
                "沙箱进程启动失败。",
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
        if exit_code is not None and exit_code != 0:
            # sandbox-exec returns non-zero when the sandbox cannot be
            # applied (e.g. restricted macOS). Never run outside it.
            raise AgentPlatformError(
                "sandbox_unavailable",
                "sandbox-exec 无法应用沙箱，已拒绝执行。",
                retryable=False,
            )
        stdout = _decode_output(stdout_bytes, request.policy.max_output_characters)
        stderr = _decode_output(stderr_bytes, request.policy.max_output_characters)
        committed = exit_code is not None
        committed_at = _now_iso() if committed else None
        receipt = EffectReceipt(
            effect_id=request.effect_id,
            tool_call_id=request.tool_call_id,
            effect_type="process",
            target=request.cwd,
            started_at=request.requested_at,
            outcome=EffectOutcome.COMMITTED if committed else EffectOutcome.UNKNOWN,
            backend="local-seatbelt",
            safe_summary=(
                f"沙箱进程已执行，退出码 {exit_code}。"
                if committed
                else "沙箱进程超时或状态未知，结果需人工核对。"
            ),
            committed_at=committed_at,
            idempotency_key=request.idempotency_key,
        )
        return ProcessResult(
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            receipt=receipt,
            truncated=stdout.truncated or stderr.truncated,
        )

    async def checkpoint(self, request: CheckpointRequest) -> CheckpointRef:
        return await self._local.checkpoint(request)

    async def restore(self, request: RestoreRequest) -> RestoreResult:
        return await self._local.restore(request)


def _sandbox_environment(request: ProcessRequest) -> Optional[dict[str, str]]:
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SeatbeltBackend", "build_seatbelt_profile", "probe_seatbelt"]
