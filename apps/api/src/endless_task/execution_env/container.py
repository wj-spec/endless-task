"""Container execution backend (M3B AP-307).

Runs ``run_process`` inside a container so process side effects are confined
to the mounted workspace by construction:

- the workspace root is mounted read-write at a fixed container path and the
  process runs with that as its working directory,
- networking is disabled unless the policy says ``ALLOW_ALL``,
- no host paths other than the workspace (and the read-only image layers)
  are reachable for writes.

The backend requires a container runtime (``docker``); when it is missing or
cannot run, execution fails closed with ``container_unavailable`` — there is
no silent fallback to the unrestricted local backend. File read/mutate and
checkpoint/restore stay on the local backend (the container confines process
side effects).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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

CONTAINER_WORKSPACE = "/workspace"


def build_container_command(
    *,
    workspace_root: str,
    image: str,
    network_mode: NetworkMode = NetworkMode.DENY,
    cwd: str = ".",
) -> tuple[str, ...]:
    """Deterministic ``docker run`` command for one execution policy."""
    if not isinstance(network_mode, NetworkMode):
        raise AgentPlatformError(
            "invalid_execution_value",
            "network_mode must use a protocol enum value",
        )
    if not isinstance(image, str) or not image.strip():
        raise AgentPlatformError(
            "invalid_container_config",
            "container image must be non-empty",
        )
    argv = [
        "run",
        "--rm",
        "--network",
        "none" if network_mode is not NetworkMode.ALLOW_ALL else "bridge",
        "-v",
        f"{workspace_root}:{CONTAINER_WORKSPACE}",
        "-w",
        str(Path(CONTAINER_WORKSPACE) / cwd) if cwd != "." else CONTAINER_WORKSPACE,
        image,
    ]
    return tuple(argv)


def probe_container_runtime(runtime: str) -> bool:
    """Whether the container runtime can actually run here."""
    try:
        result = subprocess.run(
            [runtime, "version"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class ContainerExecutionBackend(ExecutionEnvironment):
    """Local backend whose process execution runs inside a container."""

    def __init__(
        self,
        *,
        image: str,
        runtime: Optional[str] = None,
        receipt_sink: Optional[object] = None,
        checkpoint_store: Optional[Path] = None,
        ledger: Optional[object] = None,
    ) -> None:
        if not isinstance(image, str) or not image.strip():
            raise AgentPlatformError(
                "invalid_container_config",
                "container image must be non-empty",
            )
        self._image = image
        self._runtime = runtime if runtime is not None else shutil.which("docker")
        self._local = LocalExecutionBackend(
            receipt_sink=receipt_sink,
            checkpoint_store=checkpoint_store,
            ledger=ledger,
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
        if not self._runtime:
            raise AgentPlatformError(
                "container_unavailable",
                "容器运行时不可用，拒绝在非受限环境执行。",
                retryable=False,
            )
        command = build_container_command(
            workspace_root=request.policy.workspace_root,
            image=self._image,
            network_mode=request.policy.network_mode,
            cwd=request.cwd,
        )
        argv = (self._runtime, *command, *request.argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=_container_environment(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            raise AgentPlatformError(
                "process_spawn_failed",
                "容器进程启动失败。",
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
            raise AgentPlatformError(
                "container_unavailable",
                "容器运行时无法执行，已拒绝。",
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
            backend="local-container",
            safe_summary=(
                f"容器进程已执行，退出码 {exit_code}。"
                if committed
                else "容器进程超时或状态未知，结果需人工核对。"
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


def _container_environment(request: ProcessRequest) -> Optional[dict[str, str]]:
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


__all__ = [
    "CONTAINER_WORKSPACE",
    "ContainerExecutionBackend",
    "build_container_command",
    "probe_container_runtime",
]
