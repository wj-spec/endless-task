"""run_shell 工具（R5.12c）：工作区根内执行 bash 命令。

- 无状态 spawn、硬超时 + 软超时、输出上限截断、终端净化、凭据剔除。
- 危险命令恒显式确认（提权模式也不放行）。
- 执行成功落副作用日志并返回 EffectReceipt；日志失败标记 unknown_outcome。
"""

from __future__ import annotations

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
from .effect_log import EffectLog, EffectReceipt
from .resolver import WorkspaceResolver
from .shell_runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
    DEFAULT_SHELL_TIMEOUT_SECONDS,
    ShellResult,
    run_shell_command,
)

_TRUNCATION_NOTICE = "\n[输出已截断，完整输出见工作区设置 → 命令历史]"


class RunShellTool:
    definition = ToolDefinition(
        name="run_shell",
        description=(
            "在当前工作区根目录执行一条 bash 命令（非交互、无持久 shell 状态）。"
            "输出有上限，超时或输出无变化会被中止。部分危险命令始终需要用户确认。"
            "执行外部网络请求、删除、git push 等操作前，应说明意图并等待用户确认。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 16_384},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=DEFAULT_SHELL_TIMEOUT_SECONDS,
        max_output_characters=32_000,
    )

    def __init__(
        self,
        resolver: WorkspaceResolver,
        effect_log: EffectLog,
        *,
        timeout_seconds: float = DEFAULT_SHELL_TIMEOUT_SECONDS,
        no_change_timeout_seconds: float = DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._timeout_seconds = timeout_seconds
        self._no_change_timeout_seconds = no_change_timeout_seconds
        self._max_output_bytes = max_output_bytes

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在执行命令",
            completed="命令执行完成",
            failed="命令执行失败",
            cancelled="命令已停止",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return is_dangerous(str(call.arguments.get("command") or ""))

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        command = str(call.arguments.get("command") or "?")
        dangerous = is_dangerous(command)
        return ToolApprovalPrompt(
            summary=(
                "允许执行 run_shell 吗？（危险命令）"
                if dangerous
                else "允许执行 run_shell 吗？"
            ),
            reason=(
                f"将在工作区根目录执行：{command}\n"
                + (
                    "该命令属于危险名单（删除/外发/提权等），即使已开启提权模式也需要确认。"
                    if dangerous
                    else "命令在工作区目录内执行，有超时与输出上限保护。"
                )
            ),
            metadata={"toolName": "run_shell", "command": command},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        command = str(call.arguments["command"])
        result: ShellResult = await run_shell_command(
            command=command,
            cwd=binding.root,
            timeout_seconds=self._timeout_seconds,
            no_change_timeout_seconds=self._no_change_timeout_seconds,
            max_output_bytes=self._max_output_bytes,
        )
        return await self._finish(call, result, command, binding)

    async def execute_with_progress(
        self,
        call: ToolCall,
        token: CancellationToken,
        *,
        on_progress,
    ) -> ToolResult:
        """长命令执行中按间隔上报进度(可选能力,协调器检测到即启用)。"""
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        command = str(call.arguments["command"])
        result: ShellResult = await run_shell_command(
            command=command,
            cwd=binding.root,
            timeout_seconds=self._timeout_seconds,
            no_change_timeout_seconds=self._no_change_timeout_seconds,
            max_output_bytes=self._max_output_bytes,
            on_progress=on_progress,
        )
        return await self._finish(call, result, command, binding)

    async def _finish(
        self,
        call: ToolCall,
        result: ShellResult,
        command: str,
        binding,
    ) -> ToolResult:
        combined = result.stdout
        if result.stderr:
            combined += ("\n" if combined else "") + f"[stderr]\n{result.stderr}"
        if len(combined) > self.definition.max_output_characters:
            combined = (
                combined[: self.definition.max_output_characters]
                + _TRUNCATION_NOTICE
            )
            result = ShellResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                no_change_timeout=result.no_change_timeout,
                truncated=True,
                duration_seconds=result.duration_seconds,
            )
        if not combined.strip():
            combined = "（命令无输出）"
        if result.exit_code is not None and result.exit_code != 0:
            combined += (
                f"\n[退出码 {result.exit_code}"
                + ("，超时中止" if result.timed_out else "")
                + "]"
            )
        elif result.timed_out:
            combined += (
                "\n[命令超时"
                + ("：输出无变化" if result.no_change_timeout else "")
                + "，已中止；可向用户说明后调整重试]"
            )
        now_iso = _now_iso()
        receipt = EffectReceipt(
            kind="shell",
            path=command,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            truncated=result.truncated,
            executed_at=now_iso,
        )
        try:
            self._effect_log.append(
                conversation_id=call.conversation_id,
                workspace_id=binding.workspace_id,
                workspace_root=str(binding.root),
                operation="run_shell",
                detail=command,
                receipt=receipt,
                duration_ms=int(result.duration_seconds * 1000),
            )
        except ToolError:
            receipt = EffectReceipt(
                kind="shell",
                path=command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                truncated=result.truncated,
                executed_at=now_iso,
                unknown_outcome=True,
            )
            combined += "\n[副作用已发生，但本地审计日志写入失败；请到工作区设置核对命令历史]"
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=combined,
            structured_content={
                "exitCode": result.exit_code,
                "timedOut": result.timed_out,
                "noChangeTimeout": result.no_change_timeout,
                "truncated": result.truncated,
                "durationMs": int(result.duration_seconds * 1000),
                "effect": receipt.as_dict(),
            },
        )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
