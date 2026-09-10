"""S8 模型侧生态安装工具：``skill_install``。

权限模型（05 §5）：

- 协议要求外部动作工具必须 REQUIRED，所以审批恒在；区别在于**是否强制逐次确认**：
  ``ENDLESS_TASK_SKILL_INSTALL=ask``（默认）→ 每次都要人工确认；
  ``allow`` → 用户已全局授权，走信任策略免确认；
  ``deny`` → 直接拒绝。
- **静态扫描门禁恒在**：命中 high/critical 的技能无论权限如何都拒绝激活。
- 安装成功后记录 provenance（来源/摘要/扫描结论）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
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


class SkillInstallTool:
    definition = ToolDefinition(
        name="skill_install",
        description=(
            "从生态（skills.sh / GitHub）安装技能包。先 skill_search 看本地有没有；"
            "本地确实没有时用 skill_search(scope=\"ecosystem\") 找到 owner/repo@skill，"
            "再调用本工具安装（用户直接给了来源时也可以直接用）。"
            "安装会下载到暂存区、规整成技能包格式并做静态安全扫描，"
            "高风险技能会被拒绝；是否免逐次确认由用户的安装权限设置决定。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 200,
                    "description": "owner/repo 或 owner/repo@skill。",
                },
                "allow_upgrade": {
                    "type": "boolean",
                    "description": "同名技能内容不同时是否覆盖升级（默认 false）。",
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=180.0,
        max_output_characters=4_000,
    )

    def __init__(
        self,
        skill_service,
        provenance_repository,
        *,
        mode: str = "ask",
        target_root: Optional[Path] = None,
    ) -> None:
        self._skill_service = skill_service
        self._provenance_repository = provenance_repository
        self._mode = mode
        self._target_root = target_root

    def install_root(self) -> Path:
        """安装目标根：应用自己的可写技能目录。

        共享目录（``~/.claude/skills`` 等）在 S4 里是**只读**的（别的 agent 也在用），
        所以这里绝不能落到 ``skill_roots()[0]``；只有调用方没给目标时才退化。
        """
        if self._target_root is not None:
            return Path(self._target_root)
        return self._skill_service.skill_roots()[0]

    @property
    def mode(self) -> str:
        return self._mode

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在从生态安装技能",
            completed="技能安装完成",
            failed="技能安装失败",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        """``allow`` 模式下用户已全局授权，可免逐次确认；``ask`` 模式必须确认。"""
        del call
        return self._mode != "allow"

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        source = call.require_argument("source", str)
        target = self.install_root()
        return ToolApprovalPrompt(
            summary="允许从生态安装技能吗？",
            reason=(
                f"将从 GitHub 下载技能包「{source}」并安装到 {target}。\n"
                "安装前会做静态安全扫描；命中高风险会直接拒绝。"
                "装进来的技能是外部指令文本，之后会被注入到你的上下文里。"
                "（共享目录如 ~/.claude/skills 不会被写入。）"
            ),
            metadata={
                "toolName": "skill_install",
                "source": source,
                "target": str(target),
            },
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        from endless_task.skills import ImportFailure, SkillScope, import_skill_package
        from endless_task.skills.ecosystem import (
            download_skill_package,
            normalize_ecosystem_package,
        )

        if self._mode == "deny":
            raise ToolError(
                "skill_install_disabled",
                "当前配置禁止从生态安装技能（ENDLESS_TASK_SKILL_INSTALL=deny）。",
                retryable=False,
            )
        source = call.require_argument("source", str)
        allow_upgrade = call.optional_argument("allow_upgrade", bool, False)
        target_root = self.install_root()
        target_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="skill-install-tool-"))
        try:
            try:
                source_dir = await asyncio.to_thread(
                    download_skill_package, source, staging
                )
            except ValueError as error:
                raise ToolError(
                    "invalid_source", str(error), retryable=False
                ) from error
            try:
                # 生态技能多为 v1（只有 name/description），规整成 v2 再扫描。
                normalized, folded_keys = await asyncio.to_thread(
                    normalize_ecosystem_package, source_dir
                )
            except ValueError as error:
                raise ToolError(
                    "invalid_skill_package", str(error), retryable=False
                ) from error
            try:
                result = await asyncio.to_thread(
                    import_skill_package,
                    source_dir,
                    target_root,
                    allow_upgrade=allow_upgrade,
                )
            except ImportFailure as failure:
                raise ToolError(failure.code, failure.message, retryable=False) from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        token.raise_if_cancelled()
        worst_level = (
            result.scan_report.worst_level.value
            if result.scan_report is not None
            and result.scan_report.worst_level is not None
            else None
        )
        provenance = None
        if self._provenance_repository is not None:
            try:
                provenance = self._provenance_repository.record(
                    scope=SkillScope.USER.value,
                    workspace_id="",
                    name=result.name,
                    spec=source,
                    source=source.split("@", 1)[0],
                    digest=result.digest,
                    worst_level=worst_level,
                )
            except Exception:  # noqa: BLE001 记录失败不影响安装结果
                provenance = None
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"已安装技能 {result.name}@{result.version}"
                f"（来源 {source}"
                + (f"，已覆盖升级" if result.upgraded else "")
                + f"）。扫描风险等级：{worst_level or '无'}。"
                + (
                    f"（生态包已规整为 v2：折进 metadata 的键 {list(folded_keys)}。）"
                    if normalized
                    else ""
                )
                + "现在可以用 read_skill_file(name) 读取正文；"
                "如需它进入默认技能目录，请让用户固定该技能。"
            ),
            structured_content={
                "name": result.name,
                "version": result.version,
                "digest": result.digest,
                "upgraded": result.upgraded,
                "worstLevel": worst_level,
                "normalized": normalized,
                "foldedKeys": list(folded_keys),
                "provenance": provenance.as_dict() if provenance is not None else None,
            },
        )


class SkillInstallTrustPolicy:
    """S8：把 ``allow`` 模式接进执行协调器的信任策略 seam。

    只对 ``skill_install`` 生效，且**不豁免** ``force_confirm``（由协调器保证）。
    """

    def __init__(self, *, mode: str) -> None:
        self._mode = mode

    def allows(self, tool_name: str, call: Optional[ToolCall] = None) -> bool:
        del call
        return tool_name == "skill_install" and self._mode == "allow"
