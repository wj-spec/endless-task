from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

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

#: M2：允许落盘的图片类型（与模型侧附件能力对齐）。
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

if TYPE_CHECKING:
    from endless_task.tool_platform import AgentToolV2


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
        workspace_root_provider=None,
        max_image_bytes: int = 5_000_000,
    ) -> None:
        self.server_name = server_name
        self.raw_name = raw_name
        self.public_name = public_tool_name(server_name, raw_name)
        self._session_provider = session_provider
        self._effect_log = effect_log
        self._failure_callback = failure_callback
        #: M2：把 MCP 返回的图片落到工作区（内容寻址），工具结果里给引用。
        self._workspace_root_provider = workspace_root_provider
        self._max_image_bytes = max_image_bytes
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

        started = time.monotonic()
        parts: list[str] = []
        attachments: list[dict[str, object]] = []
        for block in result.content:
            block_type = getattr(block, "type", "unknown")
            if block_type == "text":
                parts.append(str(getattr(block, "text", "")))
            elif block_type == "image":
                reference = self._store_image(block, call)
                if reference is None:
                    parts.append("[图片内容无法保存：格式不支持或超过大小上限]")
                else:
                    attachments.append(reference)
                    parts.append(
                        "[图片已保存到工作区: "
                        f"{reference['path']} ({reference['mimeType']}, "
                        f"{reference['byteSize']} bytes)]"
                    )
            elif block_type == "resource_link":
                name = str(getattr(block, "name", "") or "")
                uri = str(getattr(block, "uri", "") or "")
                parts.append(f"[资源链接: {name} {uri}]".strip())
            elif block_type == "resource":
                parts.append("[嵌入资源已忽略（仅保留资源链接）]")
            else:
                parts.append(f"[未支持的 MCP 内容类型: {block_type}]")
        content = "\n".join(parts)
        truncated = len(content) > self.definition.max_output_characters
        if truncated:
            content = content[-self.definition.max_output_characters :]
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.isError:
            self._log_call(
                call,
                duration_ms=duration_ms,
                status="error",
                extra=f"isError=true; resultChars={len(content)}",
            )
            raise ToolError(
                "mcp_tool_error",
                f"MCP 工具 {self.public_name} 返回错误：{content[:1000]}",
                retryable=True,
            )
        structured: JsonValue = getattr(result, "structuredContent", None)
        self._log_call(
            call,
            duration_ms=duration_ms,
            status="ok",
            extra=(
                f"resultChars={len(content)}; truncated={truncated}; "
                f"attachments={len(attachments)}"
            ),
        )
        structured_payload = structured
        if attachments:
            if isinstance(structured, dict):
                structured_payload = {**structured, "attachments": attachments}
            else:
                structured_payload = {"result": structured, "attachments": attachments}
        return ToolResult(
            tool_call_id=call.id,
            content=content,
            structured_content=structured_payload,
            is_truncated=truncated,
        )

    def _store_image(self, block: Any, call: ToolCall) -> Optional[dict[str, object]]:
        """M2：把 MCP 图片块落到工作区 ``.endless-task/mcp-attachments``（内容寻址）。"""
        if self._workspace_root_provider is None:
            return None
        root = self._workspace_root_provider(call.conversation_id)
        if root is None:
            return None
        mime = str(getattr(block, "mimeType", "") or "").lower()
        extension = _IMAGE_EXTENSIONS.get(mime)
        if extension is None:
            return None
        raw = getattr(block, "data", None)
        if not isinstance(raw, str) or not raw:
            return None
        try:
            payload = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not payload or len(payload) > self._max_image_bytes:
            return None
        digest = hashlib.sha256(payload).hexdigest()[:16]
        relative = (
            f".endless-task/mcp-attachments/{self.server_name}/"
            f"{digest}.{extension}"
        )
        target = Path(root) / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(payload)
                temporary.replace(target)
        except OSError:
            return None
        return {
            "path": relative,
            "mimeType": mime,
            "byteSize": len(payload),
            "sha256": digest,
        }

    def _log_call(
        self,
        call: ToolCall,
        *,
        duration_ms: int = 0,
        status: str = "ok",
        extra: str = "",
    ) -> None:
        if self._effect_log is None:
            return
        receipt = EffectReceipt(
            kind="mcp_call",
            path=self.public_name,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        detail = (
            f"server={self.server_name}; rawTool={self.raw_name}; "
            f"status={status}"
        )
        if extra:
            detail = f"{detail}; {extra}"
        try:
            self._effect_log.append(
                conversation_id=call.conversation_id,
                workspace_id=None,
                workspace_root="MCP",
                operation=self.public_name,
                detail=detail,
                receipt=receipt,
                duration_ms=duration_ms,
            )
        except Exception as error:
            raise ToolError(
                "unknown_outcome",
                "MCP 工具已调用，但本地审计日志写入失败；请在能力页检查服务器状态后核对结果。",
                retryable=False,
            ) from error


def adapt_mcp_bridge_to_agent_tool(bridge: McpToolBridge) -> AgentToolV2:
    """Wrap an MCP bridge as a version-2 AgentTool for the tool platform.

    AP-105b: the version-2 catalog consumes ``AgentToolV2`` values; this keeps
    the MCP bridge as the single execution implementation and delegates to the
    legacy adapter's conservative mapping (read-only stays safe, everything
    else stays approval-required with an external-action capability).
    """
    from endless_task.tool_platform import LegacyToolAdapter

    if not isinstance(bridge, McpToolBridge):
        raise TypeError("adapt_mcp_bridge_to_agent_tool expects a McpToolBridge")
    return LegacyToolAdapter(bridge)
