"""Model-callable ``search_tools`` discovery tool (AP-106).

When the surface planner defers tools over the token budget, the model keeps
``search_tools`` to discover them on demand. Discovery is a read-only catalog
query: authorization stays in the catalog (the search context carries the
conversation capability profile), so searching can never widen the surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    plain_json,
)

from .catalog import ToolCatalog, ToolSearchQuery, ToolSummary
from .profiles import CapabilityContext
from .protocol import (
    AgentToolV2,
    IdempotencyPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

SEARCH_TOOLS_NAME = "search_tools"
_MAX_DESCRIPTION_CHARACTERS = 120


@dataclass(frozen=True)
class SearchToolsTool(AgentToolV2):
    """Discovers tools of the current authorized surface by keyword."""

    catalog: ToolCatalog
    context_provider: Callable[[], CapabilityContext]
    max_results: int = 20
    max_output_characters: int = 12_000
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not callable(getattr(self.catalog, "search", None)):
            raise AgentPlatformError(
                "invalid_tool_catalog",
                "Search tools require a ToolCatalog implementation",
            )
        if not callable(self.context_provider):
            raise AgentPlatformError(
                "invalid_search_context",
                "Search tools require a capability context provider",
            )
        if not isinstance(self.max_results, int) or not 1 <= self.max_results <= 100:
            raise AgentPlatformError(
                "invalid_search_tool_config",
                "max_results must be between 1 and 100",
            )
        if (
            not isinstance(self.max_output_characters, int)
            or self.max_output_characters <= 0
        ):
            raise AgentPlatformError(
                "invalid_search_tool_config",
                "max_output_characters must be positive",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=SEARCH_TOOLS_NAME,
                description=(
                    "按关键词在“当前可用但未注入”的工具集中搜索工具；返回名称、"
                    "能力与参数摘要，供后续调用使用。搜索是发现，不是授权。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "搜索关键词（如 read、edit、network）。",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "description": "返回条数上限，默认 20。",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.SAFE,
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        raw_query = request.arguments.get("query")
        raw_limit = request.arguments.get("limit")
        if not isinstance(raw_query, str) or not raw_query.strip():
            return _failed(
                request.call_id,
                "invalid_search_query",
                "搜索关键词必须是文本。",
            )
        if raw_limit is not None and (
            not isinstance(raw_limit, int)
            or isinstance(raw_limit, bool)
            or not 1 <= raw_limit <= 100
        ):
            return _failed(
                request.call_id,
                "invalid_search_query",
                "搜索条数必须在 1 到 100 之间。",
            )
        limit = min(raw_limit if raw_limit is not None else self.max_results, self.max_results)
        try:
            context = self.context_provider()
        except Exception:
            return _failed(
                request.call_id,
                "search_context_unavailable",
                "当前运行上下文不可用，无法搜索工具。",
            )
        if not isinstance(context, CapabilityContext):
            return _failed(
                request.call_id,
                "search_context_unavailable",
                "当前运行上下文不可用，无法搜索工具。",
            )
        try:
            query = ToolSearchQuery(
                context=context,
                query=raw_query.strip(),
                limit=limit,
            )
            summaries = self.catalog.search(query)
        except AgentPlatformError as error:
            return _failed(
                request.call_id,
                "tool_search_failed",
                error.safe_message,
            )
        request.cancellation.raise_if_cancelled()

        rows: list[Mapping[str, Any]] = []
        lines: list[str] = []
        for summary in summaries:
            row = _summary_row(summary, self._parameters_for(summary, context))
            rows.append(row)
            lines.append(_format_row(row))
        content = "\n".join(lines)
        truncated = False
        if len(content) > self.max_output_characters:
            content = content[: self.max_output_characters] + "\n…[结果已截断，请缩小搜索范围]"
            truncated = True
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=content if content else "未找到匹配工具。",
            structured_content=list(rows),
            is_truncated=truncated,
        )

    def _parameters_for(
        self,
        summary: ToolSummary,
        context: CapabilityContext,
    ) -> Optional[Mapping[str, str]]:
        try:
            tool = self.catalog.resolve(summary.name, context)
        except AgentPlatformError:
            return None
        schema = plain_json(tool.definition.input_schema)
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return None
        parameters: dict[str, str] = {}
        for name, value in properties.items():
            if isinstance(value, Mapping) and isinstance(value.get("type"), str):
                parameters[name] = str(value["type"])
        return parameters or None


def _summary_row(
    summary: ToolSummary,
    parameters: Optional[Mapping[str, str]],
) -> Mapping[str, Any]:
    description = summary.description
    if len(description) > _MAX_DESCRIPTION_CHARACTERS:
        description = description[:_MAX_DESCRIPTION_CHARACTERS] + "…"
    return {
        "name": summary.name,
        "description": description,
        "capabilities": sorted(summary.required_capabilities),
        "parameters": dict(parameters) if parameters is not None else None,
    }


def _format_row(row: Mapping[str, Any]) -> str:
    parameters = row["parameters"] or {}
    parameter_text = (
        ", ".join(f"{name}:{kind}" for name, kind in sorted(parameters.items()))
        if parameters
        else "无参数"
    )
    lines = [
        f"- {row['name']}：{row['description']}",
        f"  参数：{parameter_text}",
    ]
    if row["capabilities"]:
        lines.append(f"  能力：{', '.join(row['capabilities'])}")
    return "\n".join(lines)


def _failed(call_id: str, code: str, message: str) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.FAILED,
        content=message,
        diagnostic=SafeDiagnostic(
            code=code,
            safe_message=message,
            retryable=False,
        ),
    )


__all__ = ["SEARCH_TOOLS_NAME", "SearchToolsTool"]
