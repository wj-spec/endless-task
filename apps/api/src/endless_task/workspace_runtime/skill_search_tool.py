"""S6 模型侧技能检索工具：``skill_search``。

用途（05 §2 Level 1）：系统提示里的技能目录只列精选技能，
当任务缺少对口技能时，模型先在这里检索**全部本地技能**（含
``~/.claude/skills`` 等共享目录），拿到名称后再用
``read_skill_file(name=...)`` 读正文。

只读、自动执行、无副作用；不引入向量检索（确定性字符串匹配）。
"""

from __future__ import annotations

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

DEFAULT_MAX_RESULTS = 8


class SkillSearchTool:
    definition = ToolDefinition(
        name="skill_search",
        description=(
            "在本地技能库里检索技能（名称/描述匹配）。系统提示里只列出了精选技能，"
            "当任务缺少对口指引时用它检索全部本地技能（含共享目录），"
            "拿到技能名后用 read_skill_file(name) 读正文再执行。"
            "查询关键词中英文都可以：技能描述多为英文，中文查不到时换成英文关键词"
            "（例如「画流程图」→「mermaid diagram / flowchart」）或换个说法再查一次。"
            "scope：workspace（仅当前工作区）/ global（仅全局共享）/ all（默认，两者都查）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "要做什么（例如：画架构图 / 写周报 / 拆分学习计划）。",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "workspace", "global"],
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=8_000,
    )

    def __init__(self, resolver, skill_service, *, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self._resolver = resolver
        self._skill_service = skill_service
        self._max_results = max_results

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在检索技能库",
            completed="已检索技能库",
            failed="检索技能库失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        return ToolApprovalPrompt(
            summary="检索本地技能库",
            reason="只读操作：匹配技能名与描述，不读取正文。",
            metadata={"toolName": "skill_search"},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        query = call.require_argument("query", str)
        scope = call.optional_argument("scope", str, "all")
        if scope not in ("all", "workspace", "global"):
            raise ToolError(
                "invalid_scope", "scope 只能是 all / workspace / global。", retryable=False
            )
        binding = None
        try:
            binding = self._resolver.resolve_binding(call.conversation_id)
        except Exception:  # noqa: BLE001 未绑定也允许检索全局技能
            binding = None
        root = binding.root if binding is not None else None
        workspace_id = binding.workspace_id if binding is not None else ""
        hits = self._skill_service.search_skills(
            query,
            root,
            workspace_id=workspace_id,
            scope=scope,
            limit=self._max_results,
        )
        if not hits:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"本地技能库里没有匹配「{query}」的技能（scope={scope}）。"
                    "建议按顺序尝试：①换英文或更短的关键词再检索（技能描述多为英文）；"
                    "②放宽 scope 到 all；③直接用基础能力（文件/终端/检索）完成；"
                    "④如果用户希望扩展到生态，说明需要安装技能并请用户确认。"
                ),
                structured_content={"query": query, "scope": scope, "items": []},
            )
        lines = [
            f"- {hit.name}（{hit.scope}｜{hit.source or '本地'}）：{hit.description[:160]}"
            for hit in hits
        ]
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"匹配「{query}」的技能（{len(hits)} 条）：\n"
                + "\n".join(lines)
                + "\n用 read_skill_file(name) 读取正文后再执行。"
            ),
            structured_content={
                "query": query,
                "scope": scope,
                "items": [
                    {
                        "name": hit.name,
                        "description": hit.description,
                        "scope": hit.scope,
                        "source": hit.source,
                        "score": hit.score,
                    }
                    for hit in hits
                ],
            },
        )
