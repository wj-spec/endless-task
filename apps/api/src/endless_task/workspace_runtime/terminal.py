"""S4 内嵌终端：持久 PTY 会话服务。

设计对齐参考实现（`dsh-desktop/deepseek-harness` 的 terminal seam），但按本项目
的约束重做：

- **POSIX only**：`pty.openpty()` + `asyncio` 读主端（`loop.add_reader`，不额外起线程）；
- **有界输出**：ring buffer 保留最近 ``scrollback_lines`` / ``scrollback_bytes``，
  单次读取有上限，UTF-8 按字符安全截断；
- **归属校验**：会话按 workspace 归属，跨 workspace 访问直接拒绝；
- **上限与回收**：每工作区 ≤ ``max_sessions_per_workspace``，空闲 ``idle_timeout_seconds``
  自动关闭，服务停止时全部关闭并 kill 进程组；
- **原始字节**：输出给前端的是**未净化的原始文本**（含转义序列），交给 xterm.js 仿真；
  模型侧的行模式读取是 S5 的事。
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal as signal_module
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

logger = logging.getLogger(__name__)

DEFAULT_SHELL = "/bin/bash"
DEFAULT_SHELL_ARGS = ("--noprofile", "--norc", "-i")
DEFAULT_ROWS = 40
DEFAULT_COLS = 160
DEFAULT_SCROLLBACK_LINES = 10_000
DEFAULT_SCROLLBACK_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 256 * 1024
DEFAULT_MAX_SESSIONS_PER_WORKSPACE = 3
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
MAX_INPUT_BYTES = 64 * 1024

#: bash 每轮提示符前打印的私有 marker（S5 的就绪判定复用；xterm.js 会忽略未知 OSC）。
PROMPT_MARKER_COMMAND = "printf '\\033]133;D;\\007'"

#: bash 提示符 marker 的字节形式（就绪判定用）。
PROMPT_MARKER = "\x1b]133;D;\x07"

TerminalStatusKind = Literal["running", "exited"]


@dataclass(frozen=True)
class TerminalStatus:
    kind: TerminalStatusKind
    exit_code: Optional[int] = None
    signal: Optional[str] = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "exitCode": self.exit_code,
            "signal": self.signal,
        }


@dataclass(frozen=True)
class TerminalSnapshot:
    session_id: str
    workspace_id: str
    pid: int
    cwd: str
    name: str
    created_at: float
    status: TerminalStatus
    rows: int
    cols: int

    def as_dict(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "workspaceId": self.workspace_id,
            "pid": self.pid,
            "cwd": self.cwd,
            "name": self.name,
            "createdAt": self.created_at,
            "status": self.status.as_dict(),
            "rows": self.rows,
            "cols": self.cols,
        }


@dataclass(frozen=True)
class TerminalReadResult:
    text: str
    total_lines: int
    line_begin: int
    line_end: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "totalLines": self.total_lines,
            "lineBegin": self.line_begin,
            "lineEnd": self.line_end,
            "truncated": self.truncated,
        }


def _sanitized_terminal_env() -> dict[str, str]:
    """PTY 环境：保留颜色与分页控制，剔除凭据类变量。"""
    from .shell_runner import _sanitized_env

    env = _sanitized_env()
    env["TERM"] = "xterm-256color"
    env.pop("NO_COLOR", None)
    env["PROMPT_COMMAND"] = PROMPT_MARKER_COMMAND
    env.setdefault("PS1", "\\w \\$ ")
    return env


class PtySession:
    """一个持久 PTY 会话（工作区隔离、有界输出、可回收）。"""

    def __init__(
        self,
        *,
        workspace_id: str,
        cwd: Path,
        name: str = "",
        shell: str = DEFAULT_SHELL,
        shell_args: tuple[str, ...] = DEFAULT_SHELL_ARGS,
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
        scrollback_lines: int = DEFAULT_SCROLLBACK_LINES,
        scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        session_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id or f"term_{uuid.uuid4().hex[:16]}"
        self.workspace_id = workspace_id
        self.cwd = Path(cwd).expanduser()
        self.name = name
        self.rows = rows
        self.cols = cols
        self.created_at = time.time()
        self.last_active_at = self.created_at
        self._shell = shell
        self._shell_args = tuple(shell_args)
        self._scrollback_lines = scrollback_lines
        self._scrollback_bytes = scrollback_bytes
        self._max_read_bytes = max_read_bytes
        self._master_fd: Optional[int] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._decoder = _IncrementalUtf8()
        self._chunks: list[str] = []
        self._buffer_bytes = 0
        self._buffer_dropped = False
        self._status = TerminalStatus(kind="running")
        self._listeners: list[Callable[[str], None]] = []
        self._exit_listeners: list[Callable[[TerminalStatus], None]] = []
        self._waiters: list[asyncio.Future[TerminalStatus]] = []
        self._closing = False
        self._closed = False
        self._prompt_seen = False
        self._prompt_waiter: Optional[asyncio.Future[bool]] = None
        self._watch_task: Optional[asyncio.Task] = None

    # ---------- 生命周期 ----------

    async def start(self) -> TerminalSnapshot:
        if self._process is not None:
            raise RuntimeError("PTY session already started")
        if not self.cwd.is_dir():
            raise ValueError("工作区目录不可用。")
        master_fd, slave_fd = pty.openpty()
        self._set_window_size(master_fd, self.rows, self.cols)
        env = _sanitized_terminal_env()
        try:
            process = await asyncio.create_subprocess_exec(
                self._shell,
                *self._shell_args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.cwd),
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            os.close(master_fd)
            os.close(slave_fd)
            raise ValueError(f"无法启动终端：{error}") from error
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self._master_fd = master_fd
        self._process = process
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master_fd, self._on_readable)
        self._watch_task = asyncio.create_task(self._watch_process())
        return self.snapshot()

    async def close(self, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closing = True
        if self._loop is not None and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        process = self._process
        if process is not None and process.returncode is None:
            # 交互式 shell 会忽略 SIGTERM，先发 SIGHUP（挂断）再升级到 SIGKILL。
            self._signal_process_group(signal_module.SIGHUP)
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._signal_process_group(signal_module.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    self._kill_process_group()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "PTY process did not exit after SIGKILL: %s",
                            self.session_id,
                        )
        if self._status.kind == "running":
            self._set_status(TerminalStatus(kind="exited", exit_code=None))
        self._closed = True
        self._resolve_waiters(self._status)
        logger.info("PTY session closed (%s): %s", reason, self.session_id)

    # ---------- 输入 / 尺寸 / 信号 ----------

    def write(self, data: str) -> None:
        if self._master_fd is None or self._status.kind != "running":
            return
        encoded = data.encode("utf-8")[:MAX_INPUT_BYTES]
        self.last_active_at = time.time()
        try:
            os.write(self._master_fd, encoded)
        except OSError:
            logger.debug("PTY write failed: %s", self.session_id, exc_info=True)

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self.rows = min(int(rows), 1000)
        self.cols = min(int(cols), 1000)
        if self._master_fd is not None:
            self._set_window_size(self._master_fd, self.rows, self.cols)

    def signal(self, name: str) -> bool:
        """只给**前台进程组**发信号（不是整个进程树）。"""
        if self._master_fd is None:
            return False
        try:
            number = getattr(signal_module, name)
        except AttributeError:
            return False
        try:
            pgid = os.tcgetpgrp(self._master_fd)
        except OSError:
            return False
        try:
            os.killpg(pgid, number)
        except OSError:
            return False
        self.last_active_at = time.time()
        return True

    # ---------- 输出 ----------

    def read(self, *, offset: int = 0, count: int = 500) -> TerminalReadResult:
        text = "".join(self._chunks)
        lines = text.split("\n")
        total = len(lines) if text else 0
        if offset < 0 or count <= 0:
            raise ValueError("offset/count 非法。")
        if offset >= total:
            return TerminalReadResult(
                text="", total_lines=total, line_begin=offset, line_end=offset,
                truncated=self._buffer_dropped,
            )
        end = total - offset
        start = max(0, end - count)
        selected = "\n".join(lines[start:end])
        bounded = _utf8_tail(selected, self._max_read_bytes)
        returned = len(bounded.split("\n")) if bounded else 0
        return TerminalReadResult(
            text=bounded,
            total_lines=total,
            line_begin=offset,
            line_end=offset + returned,
            truncated=self._buffer_dropped or len(bounded) < len(selected),
        )

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def add_exit_listener(
        self, listener: Callable[[TerminalStatus], None]
    ) -> Callable[[], None]:
        self._exit_listeners.append(listener)

        def remove() -> None:
            if listener in self._exit_listeners:
                self._exit_listeners.remove(listener)

        return remove

    def snapshot(self) -> TerminalSnapshot:
        return TerminalSnapshot(
            session_id=self.session_id,
            workspace_id=self.workspace_id,
            pid=self._process.pid if self._process is not None else 0,
            cwd=str(self.cwd),
            name=self.name,
            created_at=self.created_at,
            status=self._status,
            rows=self.rows,
            cols=self.cols,
        )

    @property
    def status(self) -> TerminalStatus:
        return self._status

    @property
    def shell_pgid(self) -> Optional[int]:
        """shell 自己的进程组（会话首进程的 pgid）。"""
        process = self._process
        if process is None:
            return None
        try:
            return os.getpgid(process.pid)
        except OSError:
            return None

    @property
    def foreground_pgid(self) -> Optional[int]:
        """PTY 当前前台进程组（前台命令运行时不是 shell 的 pgid）。"""
        if self._master_fd is None:
            return None
        try:
            return os.tcgetpgrp(self._master_fd)
        except OSError:
            return None

    async def wait_ready(self, timeout_seconds: float = 10.0) -> bool:
        """等待 shell 首次回到提示符（收到私有 marker）。

        marker 可能在订阅之前就已到达（读协程一直在跑），所以先查标志位，
        再等一个由读协程唤醒的 future。
        """
        if self._prompt_seen:
            return True
        if not self.alive:
            return False
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._prompt_waiter = waiter
        unsubscribe_exit = self.add_exit_listener(
            lambda _status: waiter.done() or waiter.set_result(False)
        )
        try:
            try:
                return await asyncio.wait_for(waiter, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                return False
        finally:
            unsubscribe_exit()
            if self._prompt_waiter is waiter:
                self._prompt_waiter = None

    @property
    def alive(self) -> bool:
        return self._status.kind == "running" and not self._closed

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active_at

    async def wait_closed(self) -> TerminalStatus:
        if self._status.kind == "exited":
            return self._status
        future: asyncio.Future[TerminalStatus] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiters.append(future)
        return await future

    # ---------- 内部 ----------

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            chunk = os.read(self._master_fd, 65_536)
        except BlockingIOError:
            return
        except OSError:
            self._set_status(TerminalStatus(kind="exited", exit_code=None))
            return
        if not chunk:
            return
        text = self._decoder.decode(chunk)
        if not text:
            return
        if PROMPT_MARKER in text:
            self._prompt_seen = True
            waiter = self._prompt_waiter
            if waiter is not None and not waiter.done():
                waiter.set_result(True)
        self._append(text)
        for listener in list(self._listeners):
            try:
                listener(text)
            except Exception:  # noqa: BLE001 单个订阅者异常不影响会话
                logger.debug("PTY listener failed", exc_info=True)

    def _append(self, text: str) -> None:
        self._chunks.append(text)
        self._buffer_bytes += len(text.encode("utf-8"))
        self.last_active_at = time.time()
        while self._chunks and (
            len(self._chunks) > self._scrollback_lines
            or self._buffer_bytes > self._scrollback_bytes
        ):
            removed = self._chunks.pop(0)
            self._buffer_bytes -= len(removed.encode("utf-8"))
            self._buffer_dropped = True
        # 单块过长时按字节裁剪，避免一块就撑爆缓冲。
        if self._buffer_bytes > self._scrollback_bytes and self._chunks:
            tail = _utf8_tail(self._chunks[-1], self._scrollback_bytes // 2)
            self._buffer_bytes -= len(self._chunks[-1].encode("utf-8")) - len(
                tail.encode("utf-8")
            )
            self._chunks[-1] = tail
            self._buffer_dropped = True

    async def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        code = await process.wait()
        if self._master_fd is not None:
            # 收尾：把主端剩余数据读完（非阻塞）。
            try:
                while True:
                    chunk = os.read(self._master_fd, 65_536)
                    if not chunk:
                        break
                    text = self._decoder.decode(chunk)
                    if text:
                        self._append(text)
                        for listener in list(self._listeners):
                            try:
                                listener(text)
                            except Exception:  # noqa: BLE001
                                logger.debug("PTY listener failed", exc_info=True)
            except (BlockingIOError, OSError):
                pass
        if self._status.kind == "running":
            self._set_status(TerminalStatus(kind="exited", exit_code=code))
        if self._loop is not None and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._closed = True

    def _set_status(self, status: TerminalStatus) -> None:
        if self._status.kind == "exited":
            return
        self._status = status
        for listener in list(self._exit_listeners):
            try:
                listener(status)
            except Exception:  # noqa: BLE001
                logger.debug("PTY exit listener failed", exc_info=True)
        self._resolve_waiters(status)

    def _resolve_waiters(self, status: TerminalStatus) -> None:
        for future in self._waiters:
            if not future.done():
                future.set_result(status)
        self._waiters.clear()

    def _signal_process_group(self, number: int) -> None:
        process = self._process
        if process is None:
            return
        try:
            os.killpg(os.getpgid(process.pid), number)
        except (OSError, ProcessLookupError):
            pass

    def _kill_process_group(self) -> None:
        self._signal_process_group(signal_module.SIGKILL)

    @staticmethod
    def _set_window_size(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            logger.debug("PTY window size update failed", exc_info=True)


class _IncrementalUtf8:
    """跨 chunk 的 UTF-8 解码（不切坏多字节字符）。"""

    def __init__(self) -> None:
        self._pending = b""

    def decode(self, chunk: bytes) -> str:
        data = self._pending + chunk
        if not data:
            return ""
        try:
            self._pending = b""
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        # 末尾可能是半个字符：从尾部最多回退 3 字节找到可解码前缀。
        for cut in range(1, 4):
            if cut >= len(data):
                # 整个 chunk 都可能是半个字符：全部暂存，等下一个 chunk。
                self._pending = data
                if len(self._pending) > 8:
                    pending, self._pending = self._pending, b""
                    return pending.decode("utf-8", errors="replace")
                return ""
            try:
                decoded = data[:-cut].decode("utf-8")
            except UnicodeDecodeError:
                continue
            self._pending = data[-cut:]
            return decoded
        self._pending = b""
        return data.decode("utf-8", errors="replace")


def _utf8_tail(text: str, max_bytes: int) -> str:
    if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
        return text
    chars = list(text)
    total = 0
    start = len(chars)
    while start > 0:
        size = len(chars[start - 1].encode("utf-8"))
        if total + size > max_bytes:
            break
        total += size
        start -= 1
    return "".join(chars[start:])


@dataclass
class _WorkspaceSessions:
    sessions: list[PtySession] = field(default_factory=list)


class TerminalService:
    """按工作区管理 PTY 会话（上限、回收、归属校验）。"""

    def __init__(
        self,
        *,
        max_sessions_per_workspace: int = DEFAULT_MAX_SESSIONS_PER_WORKSPACE,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        reap_interval_seconds: float = 60.0,
    ) -> None:
        self._max_sessions = max_sessions_per_workspace
        self._idle_timeout = idle_timeout_seconds
        self._reap_interval = reap_interval_seconds
        self._by_workspace: dict[str, _WorkspaceSessions] = {}
        self._by_id: dict[str, PtySession] = {}
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    async def create(
        self,
        *,
        workspace_id: str,
        cwd: Path,
        name: str = "",
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
    ) -> PtySession:
        async with self._lock:
            bucket = self._by_workspace.setdefault(workspace_id, _WorkspaceSessions())
            bucket.sessions = [item for item in bucket.sessions if item.alive]
            if len(bucket.sessions) >= self._max_sessions:
                raise ValueError(
                    f"该工作区已有 {self._max_sessions} 个终端会话，请先关闭一个。"
                )
            session = PtySession(
                workspace_id=workspace_id,
                cwd=cwd,
                name=name,
                rows=rows,
                cols=cols,
            )
            await session.start()
            bucket.sessions.append(session)
            self._by_id[session.session_id] = session
            self._ensure_reaper()
            return session

    def get(self, *, workspace_id: str, session_id: str) -> PtySession:
        session = self._by_id.get(session_id)
        if session is None or session.workspace_id != workspace_id:
            raise KeyError(session_id)
        return session

    def list_for_workspace(self, workspace_id: str) -> tuple[PtySession, ...]:
        bucket = self._by_workspace.get(workspace_id)
        if bucket is None:
            return ()
        bucket.sessions = [item for item in bucket.sessions if item.alive]
        return tuple(bucket.sessions)

    async def close(self, *, workspace_id: str, session_id: str) -> bool:
        try:
            session = self.get(workspace_id=workspace_id, session_id=session_id)
        except KeyError:
            return False
        await session.close("user")
        self._forget(session)
        return True

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        sessions = list(self._by_id.values())
        self._by_id.clear()
        self._by_workspace.clear()
        for session in sessions:
            try:
                await session.close("shutdown")
            except Exception:  # noqa: BLE001 关闭失败不阻断停机
                logger.debug("PTY close_all failed", exc_info=True)

    def _forget(self, session: PtySession) -> None:
        self._by_id.pop(session.session_id, None)
        bucket = self._by_workspace.get(session.workspace_id)
        if bucket is not None and session in bucket.sessions:
            bucket.sessions.remove(session)

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reap_interval)
                for session in list(self._by_id.values()):
                    if session.alive and session.idle_seconds > self._idle_timeout:
                        logger.info("Reaping idle PTY session: %s", session.session_id)
                        await session.close("idle")
                        self._forget(session)
                    elif not session.alive:
                        self._forget(session)
        except asyncio.CancelledError:  # pragma: no cover - 停机路径
            raise
