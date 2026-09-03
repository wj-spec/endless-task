from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.workspace_runtime.effect_log import EffectLog, EffectReceipt
from endless_task.tooling import (
    ToolActivityCopy,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
    JsonValue,
)

from .naming import public_tool_name


class McpToolBridge:
    definition: ToolDefinition

    def __init__(
        self,
        *,
        server_name: str,
        raw_name: str,
        description: str,
        input_schema: Mapping[str, Any],
        read_only: bool,
        destructive: bool,
        timeout_seconds: float,
        session_provider,
        effect_log: Optional[EffectLog] = None,
        failure_callback=None,
    ) -> None:
        self.server_name = server_name
        self.raw_name = raw_name
        self.public_name = public_tool_name(server_name, raw_name)
        self._session_provider = session_provider
        self._effect_log = effect_log
        self._failure_callback = failure_callback
        self.definition = ToolDefinition(
            name=self.public_name,
            description=description,
            input_schema=input_schema,
            effect=(
                ToolEffect.READ_ONLY
                if read_only
                else ToolEffect.EXTERNAL_ACTION
            ),
            approval_mode=(
                ToolApprovalMode.AUTO
                if read_only
                else ToolApprovalMode.REQUIRED
            ),
            timeout_seconds=timeout_seconds,
            max_output_characters=200_000,
        )
        self._destructive = destructive

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running=f"正在调用 MCP 工具 {self.public_name}",
            completed=f"MCP 工具 {self.public_name} 调用完成",
            failed=f"MCP 工具 {self.public_name} 调用失败",
            cancelled=f"MCP 工具 {self.public_name} 调用已停止",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return (
            self.definition.approval_mode is ToolApprovalMode.REQUIRED
            or self._destructive
        )

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        try:
            arguments = json.dumps(
                dict(call.arguments), ensure_ascii=False, sort_keys=True
            )
        except (TypeError, ValueError):
            arguments = "<无法序列化的参数>"
        return ToolApprovalPrompt(
            summary=f"允许调用 MCP 工具 {self.public_name} 吗？",
            reason=(
                f"该工具来自 MCP 服务器 {self.server_name}，"
                f"协议工具名为 {self.raw_name}。\n参数：{arguments}"
            ),
            metadata={
                "serverName": self.server_name,
                "toolName": self.public_name,
            },
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        session = self._session_provider(self.server_name)
        if session is None:
            raise ToolError(
                "mcp_server_disconnected",
                f"MCP 服务器 {self.server_name} 当前未连接，无法调用工具。",
                retryable=True,
            )
        tool_task = asyncio.create_task(
            session.call_tool(
                self.raw_name,
                dict(call.arguments),
                read_timeout_seconds=timedelta(
                    seconds=self.definition.timeout_seconds
                ),
            )
        )
        cancellation_task = asyncio.create_task(token.wait())
        done, _ = await asyncio.wait(
            {tool_task, cancellation_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            tool_task.cancel()
            try:
                await tool_task
            except asyncio.CancelledError:
                pass
            raise RuntimeCancelled()
        cancellation_task.cancel()
        try:
            result = await tool_task
        except asyncio.TimeoutError as error:
            raise ToolError(
                "mcp_tool_timeout",
                f"MCP 工具 {self.public_name} 执行超时。",
                retryable=True,
            ) from error
        except Exception as error:
            if self._failure_callback is not None:
                try:
                    self._failure_callback(self.server_name)
                except Exception:
                    pass
            raise ToolError(
                "mcp_tool_failed",
                f"MCP 工具 {self.public_name} 调用失败：{type(error).__name__}",
                retryable=True,
            ) from error

        parts: list[str] = []
        for block in result.content:
            block_type = getattr(block, "type", "unknown")
            if block_type == "text":
                parts.append(str(getattr(block, "text", "")))
            else:
                parts.append(f"[未支持的 MCP 内容类型: {block_type}]")
        content = "\n".join(parts)
        truncated = len(content) > self.definition.max_output_characters
        if truncated:
            content = content[-self.definition.max_output_characters :]
        if result.isError:
            raise ToolError(
                "mcp_tool_error",
                f"MCP 工具 {self.public_name} 返回错误：{content[:1000]}",
                retryable=True,
            )
        structured: JsonValue = getattr(result, "structuredContent", None)
        self._log_call(call)
        return ToolResult(
            tool_call_id=call.id,
            content=content,
            structured_content=structured,
            is_truncated=truncated,
        )

    def _log_call(self, call: ToolCall) -> None:
        if self._effect_log is None:
            return
        receipt = EffectReceipt(
            kind="mcp_call",
            path=self.public_name,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            self._effect_log.append(
                conversation_id=call.conversation_id,
                workspace_id=None,
                workspace_root="MCP",
                operation=self.public_name,
                detail=f"server={self.server_name}; rawTool={self.raw_name}",
                receipt=receipt,
            )
        except Exception as error:
            raise ToolError(
                "unknown_outcome",
                "MCP 工具已调用，但本地审计日志写入失败；请在能力页检查服务器状态后核对结果。",
                retryable=False,
            ) from error
