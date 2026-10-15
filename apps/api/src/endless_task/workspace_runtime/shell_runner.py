"""run_shell 执行器：无状态 bash -c、双超时、输出上限、终端净化。

参考 deepseek-harness bash-local（无状态 spawn、硬超时 + 终端净化）与
OpenHands（软超时：输出无变化即中止）。不做 OS 级沙箱（留 P7），
安全由权限模型 + 危险命令名单承担。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from endless_task.tooling import ToolError

DEFAULT_SHELL_TIMEOUT_SECONDS = 120.0
MAX_SHELL_TIMEOUT_SECONDS = 600.0
DEFAULT_NO_CHANGE_TIMEOUT_SECONDS = 60.0
#: 无输出看门狗的**下限**（秒）。它管的是"多久没有任何输出就认为卡死"，
#: 与总时长墙（`timeout_seconds`）是两件事。低于这个下限的阈值会把正常命令的
#: 短暂安静误判成卡死（0.2 秒——一条 `git status` 都可能先被杀掉），所以
#: (0, 下限) 的配置一律钳到这里；要真正关掉看门狗请显式设 0。
MIN_NO_CHANGE_TIMEOUT_SECONDS = 1.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
_KILL_GRACE_SECONDS = 3.0


def clamp_shell_timeout_seconds(configured: float) -> float:
    """配置的墙 → **实际生效**的墙：下限 1 秒、上限 `MAX_SHELL_TIMEOUT_SECONDS`。

    为什么抽成函数而不是各写一处表达式：这条墙有两个消费者——内层
    `run_shell_command`（真跑命令）与外层工具定义的 `timeout_seconds`
    （`execution_impl/coordinator.py` 读它做 `asyncio.wait(timeout=...)`）。
    两者必须**同源**，否则就会配出"外层 600、内层 120"这种墙与墙不一致的形态：
    配置写着 600，命令却在 120 秒被内层杀掉——用户看到的就是"设置调不动"。
    """
    return min(max(1.0, float(configured)), MAX_SHELL_TIMEOUT_SECONDS)


def resolve_no_change_timeout_seconds(configured: float) -> float:
    """无输出看门狗阈值的归一化（秒）；返回值 ``<= 0`` 表示**看门狗关闭**。

    语义表（Step 2 要求"0/负/极小都要有明确语义"）：
    - ``0`` 或负数 → 关闭看门狗（显式 opt-out；命令只剩下总时长墙兜底）；
    - ``(0, MIN_NO_CHANGE_TIMEOUT_SECONDS)`` → 钳到下限（不许退化成"一有输出
      延迟就杀"）；
    - nan/inf → 不是"关闭"的表达方式，回落到默认阈值（**不静默改变语义**）。

    这里只做归一化；"是不是合法配置"由设置层裁决（`AppSettings` 对负数/非有限值
    **指名报错**，装配期失败）。
    """
    value = float(configured)
    if not math.isfinite(value):
        return DEFAULT_NO_CHANGE_TIMEOUT_SECONDS
    if value <= 0:
        return 0.0
    return max(value, MIN_NO_CHANGE_TIMEOUT_SECONDS)


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
    on_progress: Optional[Callable[[str, Optional[float]], None]] = None,
    progress_interval_seconds: float = 5.0,
    clock: Optional[Callable[[], float]] = None,
) -> ShellResult:
    if not command or not command.strip():
        raise ToolError("invalid_command", "命令不能为空。", retryable=False)
    if len(command) > 16_384:
        raise ToolError(
            "command_too_long", "命令过长（超过 16K 字符）。", retryable=False
        )
    timeout_seconds = clamp_shell_timeout_seconds(timeout_seconds)
    no_change_timeout_seconds = resolve_no_change_timeout_seconds(
        no_change_timeout_seconds
    )
    try:
        cwd = cwd.resolve(strict=True)
    except OSError as error:
        raise ToolError(
            "workspace_unavailable",
            "工作区根目录不可访问，无法执行命令。",
            retryable=False,
        ) from error
    #: 可参数化的时间源（默认事件循环时钟）：让"到点才杀"这类语义可以在测试里用
    #: 假时钟验证，而不必真等 60/120 秒。生产路径不传 = 与今天逐字一致。
    now = clock if clock is not None else asyncio.get_running_loop().time
    started = now()
    last_progress_at = started
    if on_progress is not None:
        on_progress("命令开始执行", 0.0)
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
            on_progress=on_progress,
            progress_interval_seconds=progress_interval_seconds,
            started=started,
            clock=now,
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
    on_progress: Optional[Callable[[str, Optional[float]], None]] = None,
    progress_interval_seconds: float = 5.0,
    started: float = 0.0,
    clock: Optional[Callable[[], float]] = None,
) -> tuple[bool, bool]:
    now_fn = clock if clock is not None else asyncio.get_running_loop().time
    deadline = now_fn() + timeout_seconds
    last_output = now_fn()
    last_total = sum(len(buffer) for buffer in buffers)
    last_progress_at = now_fn()
    while True:
        if all(task.done() for task in pump_tasks):
            return (False, False)
        await asyncio.sleep(poll_seconds)
        total = sum(len(buffer) for buffer in buffers)
        now = now_fn()
        if total != last_total:
            last_total = total
            last_output = now
        elif (
            no_change_timeout_seconds > 0
            and now - last_output >= no_change_timeout_seconds
        ):
            # 阈值 <= 0 = 看门狗关闭（见 resolve_no_change_timeout_seconds）：
            # 今天 0 会在这里"一有输出延迟就杀"，那正是要消掉的退化形态。
            _terminate_process_group(process)
            await _drain_exit(process)
            return (True, True)
        if on_progress is not None and now - last_progress_at >= progress_interval_seconds:
            last_progress_at = now
            elapsed = int(now - started) if started else 0
            on_progress(f"命令仍在运行，已执行 {elapsed} 秒", None)
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
