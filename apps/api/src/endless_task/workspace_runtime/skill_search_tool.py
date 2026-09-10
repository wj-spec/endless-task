"""S6 模型侧技能检索工具：``skill_search``。

用途（05 §2 Level 1 / Level 2）：系统提示里的技能目录只列精选技能，
当任务缺少对口技能时，模型先在这里检索**全部本地技能**（含
``~/.claude/skills`` 等共享目录），拿到名称后再用
``read_skill_file(name=...)`` 读正文；本地也没有时用
``scope=ecosystem`` 检索生态（skills.sh），拿到 ``owner/repo@skill``
后交给 ``skill_install``（安装需要用户确认）。

只读、自动执行、无副作用；不引入向量检索（确定性字符串匹配）。
``scope=ecosystem`` 是唯一联网分支（只发查询字符串、不落盘），失败
降级为"生态检索不可用"的说明，不阻塞主流程。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

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
#: ``scope=ecosystem`` 分支的调用超时（npx 首次可能要先下载 CLI）。
ECOSYSTEM_TIMEOUT_SECONDS = 90.0
#: 生态检索连续失败后的冷却时间，避免同一轮反复等 npx。
ECOSYSTEM_FAILURE_COOLDOWN_SECONDS = 120.0


class SkillSearchTool:
    definition = ToolDefinition(
        name="skill_search",
        description=(
            "在本地技能库里检索技能（名称/描述匹配）。系统提示里只列出了精选技能，"
            "当任务缺少对口指引时用它检索全部本地技能（含共享目录），"
            "拿到技能名后用 read_skill_file(name) 读正文再执行。"
            "查询关键词中英文都可以：技能描述多为英文，中文查不到时换成英文关键词"
            "（例如「画流程图」→「mermaid diagram / flowchart」）或换个说法再查一次。"
            "scope：workspace（仅当前工作区）/ global（仅全局共享）/ all（默认，两者都查）"
            "/ ecosystem（联网检索 skills.sh 生态，本地和共享目录都没有对口技能时才用）。"
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
                    "enum": ["all", "workspace", "global", "ecosystem"],
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        # 本地检索是毫秒级；上限留给 ecosystem 分支的 npx 冷启动。
        timeout_seconds=ECOSYSTEM_TIMEOUT_SECONDS + 10.0,
        max_output_characters=8_000,
    )

    def __init__(
        self,
        resolver,
        skill_service,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        ecosystem_search: Optional[Callable[..., object]] = None,
    ) -> None:
        self._resolver = resolver
        self._skill_service = skill_service
        self._max_results = max_results
        self._ecosystem_search = ecosystem_search
        self._ecosystem_unavailable_until = 0.0

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        if call.arguments.get("scope") == "ecosystem":
            return ToolActivityCopy(
                running="正在检索技能生态",
                completed="已检索技能生态",
                failed="检索技能生态失败",
            )
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
        if scope not in ("all", "workspace", "global", "ecosystem"):
            raise ToolError(
                "invalid_scope",
                "scope 只能是 all / workspace / global / ecosystem。",
                retryable=False,
            )
        if scope == "ecosystem":
            return await self._search_ecosystem(call, query)
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

    async def _search_ecosystem(self, call: ToolCall, query: str) -> ToolResult:
        """Level 2：联网检索生态（只读、只发查询字符串）。"""
        from endless_task.skills.ecosystem import search_ecosystem

        if time.monotonic() < self._ecosystem_unavailable_until:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "生态检索刚刚失败过，已暂时跳过（避免重复等待）。"
                    "可以先用本地能力完成任务，或稍后再试。"
                ),
                structured_content={"query": query, "scope": "ecosystem", "items": []},
            )
        searcher = self._ecosystem_search or search_ecosystem
        try:
            result = await asyncio.to_thread(
                searcher, query, limit=self._max_results
            )
        except Exception as error:  # noqa: BLE001 网络/子进程失败都降级
            self._ecosystem_unavailable_until = (
                time.monotonic() + ECOSYSTEM_FAILURE_COOLDOWN_SECONDS
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"生态检索暂不可用（{type(error).__name__}）。"
                    "请改用本地能力完成任务，或让用户提供技能来源后再安装。"
                ),
                structured_content={"query": query, "scope": "ecosystem", "items": []},
            )
        hits = tuple(getattr(result, "hits", ()) or ())
        error_text = str(getattr(result, "error", "") or "")
        if not hits:
            self._ecosystem_unavailable_until = (
                time.monotonic() + ECOSYSTEM_FAILURE_COOLDOWN_SECONDS
            )
            reason = f"（{error_text}）" if error_text else ""
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"生态里没有匹配「{query}」的技能{reason}。"
                    "建议换更通用的英文关键词再试一次，或直接用基础能力完成。"
                ),
                structured_content={"query": query, "scope": "ecosystem", "items": []},
            )
        lines = [
            f"- {hit.spec}（{hit.installs} installs）：{hit.url}"
            for hit in hits
        ]
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"生态里匹配「{query}」的技能（{len(hits)} 条，按安装量排序）：\n"
                + "\n".join(lines)
                + "\n这些技能**还没安装**。挑最合适的一个后调用 "
                "skill_install(source=\"owner/repo@skill\") 安装（需要用户确认）；"
                "装完用 read_skill_file(name) 读正文。"
            ),
            structured_content={
                "query": query,
                "scope": "ecosystem",
                "items": [hit.as_dict() for hit in hits],
            },
        )
