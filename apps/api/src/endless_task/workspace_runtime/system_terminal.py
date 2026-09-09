"""S3 系统终端入口：在绑定的工作区目录打开用户自己的终端。

定位（见 `docs/product-improvements/workspace-files-and-terminal/02-terminal.md`）：

- 这是**用户的终端**，命令不经过应用的逐条确认——UI 必须明示；
- 打开失败不抛 500，返回 ``opened=False`` + 可读原因；
- 只负责"打开"，不注入环境、不代理输出（内嵌终端是 S4 的事）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# Linux 终端模拟器探测顺序（第一个存在即用）。
_LINUX_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ("--working-directory={cwd}",)),
    ("gnome-terminal", ("--working-directory={cwd}",)),
    ("konsole", ("--workdir", "{cwd}")),
    ("xfce4-terminal", ("--working-directory={cwd}",)),
    ("xterm", ()),
)

_WINDOWS_MESSAGE = "Windows 暂不支持从应用打开系统终端，请手动打开。"


@dataclass(frozen=True)
class TerminalLaunchResult:
    opened: bool
    cwd: str
    launcher: str = ""
    message: str = ""


def _spawn_detached(command: Sequence[str], *, cwd: Path, spawn: Callable):
    return spawn(
        list(command),
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_system_terminal(
    cwd: Path,
    *,
    platform: str = sys.platform,
    spawn: Optional[Callable] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    environment: Optional[dict[str, str]] = None,
) -> TerminalLaunchResult:
    """在 ``cwd`` 打开系统终端；失败返回 ``opened=False`` 与原因。"""
    launcher = spawn or subprocess.Popen
    resolver = which or shutil.which
    env = environment if environment is not None else os.environ
    target = Path(cwd).expanduser()
    if not target.is_dir():
        return TerminalLaunchResult(
            opened=False, cwd=str(target), message="工作区目录不可用。"
        )
    if platform == "win32" or platform.startswith("win"):
        return TerminalLaunchResult(
            opened=False, cwd=str(target), message=_WINDOWS_MESSAGE
        )
    if platform == "darwin":
        try:
            _spawn_detached(("open", "-a", "Terminal", str(target)), cwd=target, spawn=launcher)
        except OSError as error:
            return TerminalLaunchResult(
                opened=False,
                cwd=str(target),
                message=f"无法打开系统终端：{error}。",
            )
        return TerminalLaunchResult(
            opened=True, cwd=str(target), launcher="open -a Terminal"
        )

    candidates: list[tuple[str, tuple[str, ...]]] = []
    configured = (env.get("TERMINAL") or "").strip()
    if configured:
        candidates.append((configured, ()))
    candidates.extend(_LINUX_TERMINALS)
    for name, argument_template in candidates:
        executable = resolver(name)
        if not executable:
            continue
        argv = [executable, *[
            part.format(cwd=str(target)) for part in argument_template
        ]]
        if name == "xterm":
            argv = [executable, "-e", "bash"]
        try:
            _spawn_detached(argv, cwd=target, spawn=launcher)
        except OSError:
            continue
        return TerminalLaunchResult(
            opened=True, cwd=str(target), launcher=name
        )
    return TerminalLaunchResult(
        opened=False,
        cwd=str(target),
        message="未找到可用的终端程序（可设置 $TERMINAL 指定）。",
    )
