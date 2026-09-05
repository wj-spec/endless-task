"""M4B P3: apply_child_patches tool (explicit apply of an isolated child's
scratch output into the main workspace).

DR-4 flow: an isolated_write child only wrote to its scratch workspace (P1–P2).
Applying means copying chosen scratch files into the **main** workspace:

- main workspace is checkpointed first (the apply is a run-scoped agent
  change, so existing checkpoint/restore/rollback semantics cover it);
- conflicts (a main file already exists with different content) are listed
  and **never overwritten**; equal-content files are idempotent no-ops;
- only COMPLETED isolated children may apply (provider enforces);
- each applied file is written through the normal workspace Write tool
  semantics (containment + per-path lock + effect audit + run ledger), so
  the parent run's checkpoint/restore and the audit rail see the changes.

Schema/capability surface is registered only while delegation runs in
``isolated_write`` mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
)


@dataclass(frozen=True)
class ChildPatchFile:
    relative: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ChildPatchSource:
    files: tuple[ChildPatchFile, ...]
    total_bytes: int


class ApplyChildPatchesTool:
    """Copy an isolated child's scratch output into the main workspace."""

    name = "apply_child_patches"

    def __init__(
        self,
        *,
        patches_provider,
        writer,
    ) -> None:
        """``patches_provider(child_run_id) -> ChildPatchSource`` (raises an
        AgentPlatformError-shaped error with a ``code`` for refused applies);
        ``writer(call, relative, content) -> 'applied'|'equal'`` writes one
        file with full workspace Write semantics (path lock + audit + ledger
        + containment); raises ToolError(code='path_exists_conflict') when a
        main file already exists with different content."""
        self._patches_provider = patches_provider
        self._writer = writer
        self.definition = ToolDefinition(
            name=self.name,
            description=(
                "把已完成隔离写子代理的产出（仅其 scratch 工作区文件）显式应用到当前"
                "主工作区。应用前自动 checkpoint 主工作区；与主工作区已有文件冲突的"
                "路径不会被覆盖，会在结果中列出。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "childRunId": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
                "required": ["childRunId"],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=60.0,
            max_output_characters=8_000,
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return False

    async def execute(
        self,
        call: ToolCall,
        token: CancellationToken,
    ) -> ToolResult:
        token.raise_if_cancelled()
        child_run_id = call.require_argument("childRunId", str).strip()
        if not child_run_id:
            raise ToolError(
                "invalid_child_run_id",
                "childRunId 不能为空。",
                retryable=False,
            )
        try:
            source = self._patches_provider(child_run_id)
        except Exception as error:
            code = getattr(error, "code", "patch_provider_failed")
            raise ToolError(
                code,
                getattr(error, "safe_message", "无法读取子代理产出。")
                if hasattr(error, "safe_message")
                else str(error),
                retryable=False,
            ) from error
        if not isinstance(source, ChildPatchSource):
            raise ToolError(
                "invalid_patch_source",
                "子代理产出格式无效。",
                retryable=False,
            )

        applied: list[str] = []
        conflicts: list[dict[str, object]] = []
        for patch in source.files:
            token.raise_if_cancelled()
            try:
                outcome = await self._writer(call, patch.relative, patch.content)
            except ToolError as error:
                if error.code == "path_exists_conflict":
                    conflicts.append(
                        {
                            "path": patch.relative,
                            "reason": "exists_with_different_content",
                        }
                    )
                    continue
                raise
            if outcome == "equal":
                continue  # main file already matches: idempotent no-op
            applied.append(patch.relative)
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已应用子代理 {child_run_id} 的 {len(applied)} 个文件"
                + (f"；跳过冲突 {len(conflicts)} 个（未覆盖）" if conflicts else "。")
            ),
            structured_content={
                "childRunId": child_run_id,
                "applied": applied,
                "conflicts": conflicts,
            },
        )


__all__ = [
    "ApplyChildPatchesTool",
    "ChildPatchFile",
    "ChildPatchSource",
]
