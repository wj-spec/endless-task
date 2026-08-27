"""run_shell 执行器：无状态 bash -c、双超时、输出上限、终端净化。

参考 deepseek-harness bash-local（无状态 spawn、硬超时 + 终端净化）与
OpenHands（软超时：输出无变化即中止）。不做 OS 级沙箱（留 P7），
安全由权限模型 + 危险命令名单承担。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from endless_task.tooling import ToolError

DEFAULT_SHELL_TIMEOUT_SECONDS = 120.0
MAX_SHELL_TIMEOUT_SECONDS = 600.0
DEFAULT_NO_CHANGE_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
_KILL_GRACE_SECONDS = 3.0

_CREDENTIAL_KEYS = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|API_KEY)", re.IGNORECASE
)
_AWS_PREFIX = re.compile(r"^AWS_")


def _sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if _CREDENTIAL_KEYS.search(key) or _AWS_PREFIX.match(key):
            env.pop(key, None)
    env.update(
        {
            "NO_COLOR": "1",
            "TERM": "dumb",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@dataclass(frozen=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]
    timed_out: bool = False
    no_change_timeout: bool = False
    truncated: bool = False
    duration_seconds: float = 0.0


async def run_shell_command(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: float = DEFAULT_SHELL_TIMEOUT_SECONDS,
    no_change_timeout_seconds: float = DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: Optional[dict[str, str]] = None,
) -> ShellResult:
    if not command or not command.strip():
        raise ToolError("invalid_command", "命令不能为空。", retryable=False)
    if len(command) > 16_384:
        raise ToolError(
            "command_too_long", "命令过长（超过 16K 字符）。", retryable=False
        )
    timeout_seconds = min(max(1.0, timeout_seconds), MAX_SHELL_TIMEOUT_SECONDS)
    try:
        cwd = cwd.resolve(strict=True)
    except OSError as error:
        raise ToolError(
            "workspace_unavailable",
            "工作区根目录不可访问，无法执行命令。",
            retryable=False,
        ) from error
    started = asyncio.get_running_loop().time()
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        command,
        cwd=str(cwd),
        env=env if env is not None else _sanitized_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_buffer = bytearray()
    err_buffer = bytearray()
    truncated = False

    async def pump(stream, buffer) -> None:
        nonlocal truncated
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            remaining = max_output_bytes - len(buffer)
            if remaining <= 0:
                truncated = True
                continue
            buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True

    pump_tasks = [
        asyncio.create_task(pump(process.stdout, out_buffer)),
        asyncio.create_task(pump(process.stderr, err_buffer)),
    ]
    try:
        timed_out, no_change_timeout = await _wait_with_timeouts(
            process,
            pump_tasks,
            buffers=(out_buffer, err_buffer),
            timeout_seconds=timeout_seconds,
            no_change_timeout_seconds=no_change_timeout_seconds,
        )
    except asyncio.CancelledError:
        _terminate_process_group(process)
        await asyncio.gather(*pump_tasks, return_exceptions=True)
        raise
    finally:
        await asyncio.gather(*pump_tasks, return_exceptions=True)

    duration = asyncio.get_running_loop().time() - started
    return ShellResult(
        stdout=out_buffer.decode("utf-8", errors="replace"),
        stderr=err_buffer.decode("utf-8", errors="replace"),
        exit_code=process.returncode,
        timed_out=timed_out,
        no_change_timeout=no_change_timeout,
        truncated=truncated,
        duration_seconds=duration,
    )


async def _wait_with_timeouts(
    process,
    pump_tasks,
    *,
    buffers,
    timeout_seconds: float,
    no_change_timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> tuple[bool, bool]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_output = loop.time()
    last_total = sum(len(buffer) for buffer in buffers)
    while True:
        if all(task.done() for task in pump_tasks):
            return (False, False)
        await asyncio.sleep(poll_seconds)
        total = sum(len(buffer) for buffer in buffers)
        now = loop.time()
        if total != last_total:
            last_total = total
            last_output = now
        elif now - last_output >= no_change_timeout_seconds:
            _terminate_process_group(process)
            await _drain_exit(process)
            return (True, True)
        if now >= deadline:
            _terminate_process_group(process)
            await _drain_exit(process)
            return (True, False)


async def _drain_exit(process) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError):
        _kill_process_group(process, signal.SIGKILL)
        try:
            await process.wait()
        except (ProcessLookupError, asyncio.TimeoutError):
            pass


def _terminate_process_group(process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _kill_process_group(process, sig: signal.Signals) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
