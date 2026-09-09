"""S5 模型侧终端工具（6 个）：持久 PTY 会话的 open/send/read/signal/close/list。

与人用终端（S4 面板）共用同一个 `TerminalService`，但**读取路径不同**：

- 人：原始字节 → xterm.js 全仿真；
- 模型：本模块把输出裁剪成有界文本（默认 16 KiB），并给出 ``waitReason``。

审批（见 `03-model-terminal-tools.md`）：open/send/signal 为 REQUIRED（危险命令恒确认），
read/list/close 为 AUTO。默认不注册（``ENDLESS_TASK_TERMINAL_TOOLS=1`` 才开启），
避免给不用的会话平添工具面开销。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

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

from .dangerous_commands import is_dangerous
from .terminal import PROMPT_MARKER, PtySession, TerminalService

DEFAULT_MAX_RESULT_BYTES = 16 * 1024
DEFAULT_SEND_QUIET_MS = 800
DEFAULT_SEND_TIMEOUT_SECONDS = 30.0

def _bounded(text: str, max_bytes: int = DEFAULT_MAX_RESULT_BYTES) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    chars = list(text)
    total = 0
    start = len(chars)
    while start > 0:
        size = len(chars[start - 1].encode("utf-8"))
        if total + size > max_bytes:
            break
        total += size
        start -= 1
    return "".join(chars[start:]), True


class _TerminalToolBase:
    """共用：解析会话（工作区归属校验）与参数校验。"""

    def __init__(self, resolver, service: Optional[TerminalService]) -> None:
        self._resolver = resolver
        self._service = service

    def _require_service(self) -> TerminalService:
        if self._service is None:
            raise ToolError(
                "terminal_unavailable",
                "当前部署未启用内嵌终端。",
                retryable=False,
            )
        return self._service

    def _session(self, call: ToolCall) -> PtySession:
        binding = self._resolver.require_binding(call.conversation_id)
        session_id = call.require_argument("session_id", str)
        try:
            return self._require_service().get(
                workspace_id=binding.workspace_id, session_id=session_id
            )
        except KeyError:
            raise ToolError(
                "terminal_session_not_found",
                f"终端会话不存在或不属于当前工作区：{session_id}。",
                retryable=False,
            ) from None


class TerminalOpenTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_open",
        description=(
            "在工作区目录创建一个持久终端会话（PTY）。需要跨工具调用保持状态、"
            "或需要交互式 stdin 时才用；一次性命令请用 run_shell。"
            "创建后请用 terminal_send 发送命令、terminal_read 读取输出，"
            "用完记得 terminal_close。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 64},
                "rows": {"type": "integer", "minimum": 5, "maximum": 200},
                "cols": {"type": "integer", "minimum": 20, "maximum": 500},
            },
            "required": [],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=30.0,
        max_output_characters=4_000,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在创建终端会话",
            completed="已创建终端会话",
            failed="创建终端会话失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return True

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        return ToolApprovalPrompt(
            summary="允许创建终端会话吗？",
            reason=(
                "将在工作区目录启动一个持久 shell（PTY）。该会话内的命令"
                "由你后续逐条发起，每次发送仍需确认。"
            ),
            metadata={"toolName": "terminal_open"},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        service = self._require_service()
        try:
            session = await service.create(
                workspace_id=binding.workspace_id,
                cwd=Path(binding.root),
                name=call.optional_argument("name", str, "")[:64],
                rows=call.optional_argument("rows", int, 40),
                cols=call.optional_argument("cols", int, 160),
            )
        except ValueError as error:
            raise ToolError("terminal_create_failed", str(error), retryable=False) from error
        ready = await session.wait_ready(timeout_seconds=10.0)
        snapshot = session.snapshot()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已创建终端会话 {snapshot.session_id}（pid {snapshot.pid}，"
                f"cwd {snapshot.cwd}，"
                + ("已就绪" if ready else "尚未出现提示符（可能仍在初始化）")
                + "）。用 terminal_send 发送命令。"
            ),
            structured_content={"terminal": snapshot.as_dict(), "ready": ready},
        )


class TerminalSendTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_send",
        description=(
            "向持久终端会话写入文本并等待结果。默认补一个回车。"
            "返回 waitReason：stdin_read（已回到提示符）/ inferred_idle（输出安静）"
            "/ timeout / session_exit。**只有 session_exit 才代表前台命令已退出**，"
            "inferred_idle 或 timeout 时命令可能仍在运行，请用 terminal_read 继续查看。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "text": {"type": "string", "maxLength": 16_384},
                "submit": {"type": "boolean"},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["session_id", "text"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=300.0,
        max_output_characters=DEFAULT_MAX_RESULT_BYTES // 4,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在向终端发送命令",
            completed="终端命令已执行",
            failed="终端命令执行失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return True

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        text = call.require_argument("text", str)
        dangerous = is_dangerous(text)
        return ToolApprovalPrompt(
            summary=(
                "允许向终端发送命令吗？（危险命令）"
                if dangerous
                else "允许向终端发送命令吗？"
            ),
            reason=(
                f"将写入终端：{text}\n"
                + (
                    "该命令属于危险名单（删除/外发/提权等），必须确认。"
                    if dangerous
                    else "命令在持久终端会话中执行，输出有上限。"
                )
            ),
            metadata={
                "toolName": "terminal_send",
                "command": text,
                "sessionId": call.require_argument("session_id", str),
            },
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        session = self._session(call)
        if not session.alive:
            raise ToolError(
                "terminal_session_exited",
                "该终端会话已经退出，请用 terminal_open 重新创建。",
                retryable=False,
            )
        text = call.require_argument("text", str)
        submit = call.optional_argument("submit", bool, True)
        raw_timeout = call.arguments.get("timeout_seconds")
        try:
            timeout = (
                DEFAULT_SEND_TIMEOUT_SECONDS
                if raw_timeout is None
                else float(raw_timeout)
            )
        except (TypeError, ValueError):
            raise ToolError(
                "invalid_timeout", "timeout_seconds 必须是数字。", retryable=False
            ) from None
        timeout = min(max(1.0, timeout), 300.0)
        viewport, wait_reason, truncated = await _send_and_wait(
            session,
            text=f"{text}\r" if submit else text,
            timeout_seconds=timeout,
            token=token,
        )
        status = session.status
        payload = _bounded(viewport)
        note = ""
        if wait_reason != "session_exit":
            note = (
                "\n[提示：该结果不代表前台命令已退出；需要时用 terminal_read 继续读取]"
            )
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"waitReason={wait_reason}"
                f"（{'输出已截断' if payload[1] else '完整'}）\n"
                f"```terminal\n{payload[0]}\n```{note}"
            ),
            structured_content={
                "waitReason": wait_reason,
                "viewport": payload[0],
                "truncated": payload[1],
                "sessionStatus": status.as_dict(),
                "effect": {"kind": "terminal_send", "sessionId": session.session_id},
            },
        )


async def _send_and_wait(
    session: PtySession,
    *,
    text: str,
    timeout_seconds: float,
    token: CancellationToken,
    quiet_ms: int = DEFAULT_SEND_QUIET_MS,
) -> tuple[str, str, bool]:
    """写入并等待"看起来结束"：提示符 marker / 输出安静 / 超时 / 会话退出。"""
    collected: list[str] = []
    loop = asyncio.get_running_loop()
    last_output = loop.time()
    marker_seen = False

    def on_output(chunk: str) -> None:
        nonlocal last_output, marker_seen
        collected.append(chunk)
        last_output = loop.time()
        if PROMPT_MARKER in chunk:
            marker_seen = True

    unsubscribe = session.add_listener(on_output)
    unsubscribe_exit = session.add_exit_listener(lambda _status: None)
    try:
        session.write(text)
        deadline = loop.time() + timeout_seconds
        while True:
            token.raise_if_cancelled()
            if not session.alive:
                return "".join(collected), "session_exit", False
            now = loop.time()
            if now >= deadline:
                return "".join(collected), "timeout", True
            idle = now - last_output
            # 只有"看到提示符 marker"且"前台进程组回到 shell 自己"才算真的回到
            # 提示符——否则前台命令（如 sleep）仍在跑，不能报 stdin_read。
            if (
                marker_seen
                and idle >= 0.05
                and session.foreground_pgid == session.shell_pgid
            ):
                return "".join(collected), "stdin_read", False
            if idle * 1000 >= quiet_ms:
                return "".join(collected), "inferred_idle", False
            await asyncio.sleep(0.02)
    finally:
        unsubscribe()
        unsubscribe_exit()


class TerminalReadTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_read",
        description=(
            "读取持久终端会话的保留输出（不发送任何输入）。offset 为距最新行的"
            "偏移（0=最新），count 为最多返回行数；输出有上限。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "offset": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "count": {"type": "integer", "minimum": 1, "maximum": 5_000},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=DEFAULT_MAX_RESULT_BYTES // 4,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在读取终端输出",
            completed="已读取终端输出",
            failed="读取终端输出失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        return ToolApprovalPrompt(
            summary="读取终端输出",
            reason="只读操作。",
            metadata={"toolName": "terminal_read"},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        session = self._session(call)
        page = session.read(
            offset=call.optional_argument("offset", int, 0),
            count=call.optional_argument("count", int, 500),
        )
        payload = _bounded(page.text)
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"[终端 {session.session_id}：共 {page.total_lines} 行，"
                f"offset={page.line_begin}..{page.line_end}"
                f"{'，已截断' if page.truncated else ''}]\n{payload[0]}"
            ),
            structured_content={
                **page.as_dict(),
                "viewport": payload[0],
                "bounded": payload[1],
                "sessionStatus": session.status.as_dict(),
            },
        )


class TerminalSignalTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_signal",
        description=(
            "向终端会话的**前台进程组**发送信号（SIGINT/SIGTERM/SIGKILL/SIGTSTP/SIGHUP）。"
            "常用于中断正在运行的命令（SIGINT），不会杀掉 shell 本身。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "signal": {
                    "type": "string",
                    "enum": ["SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP"],
                },
            },
            "required": ["session_id", "signal"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=10.0,
        max_output_characters=2_000,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在向终端发送信号",
            completed="已向终端发送信号",
            failed="向终端发送信号失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return True

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        signal = call.require_argument("signal", str)
        return ToolApprovalPrompt(
            summary=f"允许向终端发送 {signal} 吗？",
            reason=(
                f"将向前台进程组发送 {signal}；SIGKILL/SIGHUP 可能导致会话结束。"
            ),
            metadata={
                "toolName": "terminal_signal",
                "signal": signal,
                "sessionId": call.require_argument("session_id", str),
            },
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        session = self._session(call)
        name = call.require_argument("signal", str)
        delivered = session.signal(name)
        if not delivered:
            raise ToolError(
                "terminal_signal_failed",
                "信号发送失败：会话已退出或没有前台进程组。",
                retryable=False,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"已向终端 {session.session_id} 的前台进程组发送 {name}。",
            structured_content={
                "signal": name,
                "sessionStatus": session.status.as_dict(),
            },
        )


class TerminalCloseTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_close",
        description="关闭持久终端会话并结束其进程组（幂等）。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=20.0,
        max_output_characters=2_000,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在关闭终端会话",
            completed="已关闭终端会话",
            failed="关闭终端会话失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        return ToolApprovalPrompt(
            summary="关闭终端会话",
            reason="会话内的进程会被结束。",
            metadata={"toolName": "terminal_close"},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        session_id = call.require_argument("session_id", str)
        closed = await self._require_service().close(
            workspace_id=binding.workspace_id, session_id=session_id
        )
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已关闭终端会话 {session_id}。" if closed else f"终端会话 {session_id} 不存在或已关闭。"
            ),
            structured_content={"sessionId": session_id, "closed": closed},
        )


class TerminalListTool(_TerminalToolBase):
    definition = ToolDefinition(
        name="terminal_list",
        description="列出当前工作区内的持久终端会话及其状态。",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=4_000,
    )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在列出终端会话",
            completed="已列出终端会话",
            failed="列出终端会话失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        return ToolApprovalPrompt(
            summary="列出终端会话",
            reason="只读操作。",
            metadata={"toolName": "terminal_list"},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        sessions = self._require_service().list_for_workspace(binding.workspace_id)
        items = [session.snapshot().as_dict() for session in sessions]
        if not items:
            return ToolResult(
                tool_call_id=call.id,
                content="当前工作区没有终端会话。",
                structured_content={"items": []},
            )
        lines = [
            f"- {item['sessionId']} pid={item['pid']} {item['status']['kind']}"
            for item in items
        ]
        return ToolResult(
            tool_call_id=call.id,
            content="当前工作区终端会话：\n" + "\n".join(lines),
            structured_content={"items": items},
        )


TERMINAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_signal",
        "terminal_close",
        "terminal_list",
    }
)


def build_terminal_tools(resolver, service: Optional[TerminalService]):
    """构造 6 个模型侧终端工具（调用方决定是否注册）。"""
    return (
        TerminalOpenTool(resolver, service),
        TerminalSendTool(resolver, service),
        TerminalReadTool(resolver, service),
        TerminalSignalTool(resolver, service),
        TerminalCloseTool(resolver, service),
        TerminalListTool(resolver, service),
    )
