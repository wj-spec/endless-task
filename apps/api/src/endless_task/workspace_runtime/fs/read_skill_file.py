"""`ReadSkillFileTool`（从 fs_tools.py 拆出，行为零改动）。"""

from __future__ import annotations

from ..path_safety import resolve_external_read_path
from dataclasses import replace
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import ToolActivityCopy, ToolApprovalMode, ToolCall, ToolDefinition, ToolEffect, ToolError, ToolResult
from pathlib import Path
from .common import _decode_utf8
from typing import Callable, Optional


class ReadSkillFileTool:
    definition = ToolDefinition(
        name="read_skill_file",
        description=(
            "按名称读取 <available_skills> 里列出的技能正文（传 name）。"
            "该工具只读且仅允许访问技能目录。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "技能名（与目录中的 <name> 一致）。",
                },
                "locator": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "兼容写法：skill://<scope>/<name>。",
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 2048},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "anyOf": [
                {"required": ["name"]},
                {"required": ["locator"]},
                {"required": ["path"]},
            ],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=40_000,
    )

    def __init__(
        self,
        skill_root_provider: Callable[[str], tuple[Path, ...]],
        *,
        locator_resolver_provider: Optional[
            Callable[[str], Optional[Callable[[str], Optional[Path]]]]
        ] = None,
        name_resolver_provider: Optional[
            Callable[[str], Optional[Callable[[str], Optional[Path]]]]
        ] = None,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._skill_root_provider = skill_root_provider
        self._locator_resolver_provider = locator_resolver_provider
        self._name_resolver_provider = name_resolver_provider
        self._max_file_bytes = max_file_bytes
        # 未启用 locator 解析时，不要把 locator 写成"推荐"——否则模型会先试
        # 一次必然失败的 locator 调用（S1 live 实测发生过）。
        if locator_resolver_provider is None:
            schema = dict(type(self).definition.input_schema)
            properties = dict(schema["properties"])
            properties.pop("locator", None)
            self.definition = replace(
                type(self).definition,
                description=(
                    "按名称读取 <available_skills> 里列出的技能正文（传 name）；"
                    "该工具只读且仅允许访问技能目录。"
                ),
                input_schema={
                    **schema,
                    "properties": properties,
                    "anyOf": [
                        {"required": ["name"]},
                        {"required": ["path"]},
                    ],
                },
            )

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在读取技能",
            completed="已读取技能",
            failed="读取技能失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        name = call.optional_argument("name", str, None)
        locator = call.optional_argument("locator", str, None)
        if name is not None:
            canonical = self._resolve_name(name, call)
        elif locator is not None:
            canonical = self._resolve_locator(locator, call)
        else:
            roots = self._skill_root_provider(call.conversation_id)
            resolved = resolve_external_read_path(
                roots, call.require_argument("path", str)
            )
            canonical = resolved.canonical
        if not canonical.exists():
            raise ToolError("path_not_found", f"技能文件不存在。", retryable=False)
        if not canonical.is_file():
            raise ToolError("path_is_directory", "路径指向目录。", retryable=False)
        raw = canonical.read_bytes()
        if len(raw) > self._max_file_bytes:
            raise ToolError(
                "file_too_large",
                f"技能文件超过读取上限（{self._max_file_bytes} 字节）。",
                retryable=False,
            )
        text = _decode_utf8(raw)
        lines = text.splitlines() or [""]
        start_line = call.optional_argument("start_line", int, 1)
        line_count = call.optional_argument("line_count", int, 120)
        if start_line > len(lines):
            raise ToolError(
                "file_line_out_of_range",
                f"文件只有 {len(lines)} 行，无法从第 {start_line} 行读取。",
                retryable=False,
            )
        end_line = min(len(lines), start_line + line_count - 1)
        selected = "\n".join(lines[start_line - 1 : end_line])
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"[来源：技能 {canonical}:L{start_line}-L{end_line}]\n"
                f"{selected}"
            ),
            structured_content={
                "path": str(canonical),
                "startLine": start_line,
                "endLine": end_line,
                "totalLines": len(lines),
            },
        )

    def _resolve_name(self, name: str, call: ToolCall) -> Path:
        """S3：按技能名解析（目录只暴露 name，不暴露路径）。"""
        if self._name_resolver_provider is None:
            raise ToolError(
                "name_unavailable",
                "技能名解析不可用；请改用 path。",
                retryable=False,
            )
        resolver = self._name_resolver_provider(call.conversation_id)
        if resolver is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{name}。",
                retryable=False,
            )
        try:
            canonical = resolver(name)
        except (ValueError, OSError) as error:
            raise ToolError(
                "invalid_skill_name", f"技能名无效：{name}。", retryable=False
            ) from error
        if canonical is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{name}（可能已禁用或依赖不满足）。",
                retryable=False,
            )
        return self._guard_containment(canonical, call)

    def _guard_containment(self, canonical: Path, call: ToolCall) -> Path:
        roots = self._skill_root_provider(call.conversation_id)
        resolved = resolve_external_read_path(roots, str(canonical))
        return resolved.canonical

    def _resolve_locator(self, locator: str, call: ToolCall) -> Path:
        if self._locator_resolver_provider is None:
            raise ToolError(
                "locator_unavailable",
                "技能 locator 解析未启用；请改用 path。",
                retryable=False,
            )
        resolver = self._locator_resolver_provider(call.conversation_id)
        if resolver is None:
            raise ToolError(
                "locator_unavailable",
                "技能 locator 解析未启用；请改用 path。",
                retryable=False,
            )
        try:
            canonical = resolver(locator)
        except (ValueError, OSError) as error:
            raise ToolError(
                "invalid_skill_locator",
                f"技能 locator 无效：{locator}。",
                retryable=False,
            ) from error
        if canonical is None:
            raise ToolError(
                "skill_not_found",
                f"未找到技能：{locator}。",
                retryable=False,
            )
        # Locator resolution must still satisfy root containment: re-run the
        # canonical path through the same resolver used for legacy paths and
        # return its canonical (symlink-resolved) form.
        roots = self._skill_root_provider(call.conversation_id)
        return resolve_external_read_path(roots, str(canonical)).canonical
