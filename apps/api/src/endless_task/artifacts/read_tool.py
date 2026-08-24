from __future__ import annotations

from endless_task.domain.models import ArtifactStatus
from endless_task.domain.repositories import RepositoryError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
)


class ReadArtifactTool:
    definition = ToolDefinition(
        name="read_artifact",
        description=(
            "读取本产品中已保存的 Artifact（独立文档结果）的当前内容。"
            "修改任何 Artifact 前，必须先调用本工具读取完整当前内容。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        timeout_seconds=5.0,
        max_output_characters=200_000,
    )

    def __init__(self, repository) -> None:
        self._repository = repository

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        cancellation_token.raise_if_cancelled()
        artifact_id = str(call.arguments["artifact_id"]).strip()
        try:
            artifact = self._repository.get_artifact(artifact_id)
        except RepositoryError as error:
            raise ToolError(
                "artifact_not_found",
                "没有找到对应的 Artifact。",
                retryable=False,
            ) from error
        if artifact.status is not ArtifactStatus.ACTIVE:
            raise ToolError(
                "artifact_not_found",
                "该 Artifact 已被删除。",
                retryable=False,
            )
        version = self._repository.get_current_version(artifact_id)
        source_label = f"《{artifact.title}》v{version.ordinal}"
        cancellation_token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=f"[来源：{source_label}]\n{version.content}",
            structured_content={
                "artifactId": artifact.id,
                "title": artifact.title,
                "ordinal": version.ordinal,
            },
        )
