from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from ..storage.sqlite_skill_override_repository import SqliteSkillOverrideRepository
from .loader import (
    SkillRootSpec,
    default_skill_root_specs,
    discover_from_roots,
    discover_skills,
)
from .models import Skill, SkillScope

#: 单次注入的技能正文上限（字节）；超出按字符安全截断并标注。
MAX_SKILL_BODY_BYTES = 32 * 1024

#: 显式调用失败原因（供 API 与 UI 复用）。
INVOKE_OK = "ok"
INVOKE_UNKNOWN = "unknown"
INVOKE_NOT_USER_INVOCABLE = "not_user_invocable"
INVOKE_DISABLED = "disabled"
INVOKE_INVALID = "invalid"


#: S3：目录里单条描述的上限（避免一个技能吃掉整个前缀预算）。
MAX_CATALOG_DESCRIPTION_CHARACTERS = 500

#: S4：整个技能目录的字符预算。装了共享技能后目录可能很大（41 个技能 ≈ 14KB），
#: 每轮都注入会稳定吃掉上下文，所以超预算时先压缩描述、再截断条目。
MAX_CATALOG_CHARACTERS = 12_000
_CATALOG_DESCRIPTION_STEPS = (500, 240, 160, 100, 60)


def build_available_skills_prompt(
    skills: Tuple[Skill, ...],
    *,
    locator_mode: bool = False,
) -> str:
    """技能目录：**只给名称 + 截断描述，不给路径**（S3）。

    模型通过 ``read_skill_file(name=...)`` 按需读取正文；路径是本地实现细节，
    既不该进入模型上下文，也会在技能移动/换工作区后失效。
    """
    del locator_mode  # 兼容旧调用方；目录不再暴露路径或 locator。
    visible = [skill for skill in skills if skill.valid and not skill.disable_model_invocation]
    if not visible:
        return ""
    def render(entries: tuple[Skill, ...], description_cap: int) -> str:
        lines = [
            "以下技能为特定任务提供专门指引（这里只有摘要，未读取正文前不要据其行动）。"
            "任务与某技能描述匹配时，先调用 read_skill_file（传 name）读取全文，"
            "再按其中步骤执行；技能内相对路径以其所在目录解析。",
            "<available_skills>",
        ]
        for skill in entries:
            description = skill.description.strip()
            if len(description) > description_cap:
                description = description[:description_cap].rstrip() + "…"
            lines.extend(
                [
                    "  <skill>",
                    f"    <name>{escape(skill.name)}</name>",
                    f"    <description>{escape(description)}</description>",
                    "  </skill>",
                ]
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    cap = MAX_CATALOG_DESCRIPTION_CHARACTERS
    for candidate_cap in _CATALOG_DESCRIPTION_STEPS:
        cap = candidate_cap
        text = render(tuple(visible), cap)
        if len(text) <= MAX_CATALOG_CHARACTERS:
            return text
    # 描述压到最小仍超预算：保留前 N 个，其余提示用 /技能名 显式调用。
    kept: list[Skill] = []
    for skill in visible:
        attempt = render(tuple([*kept, skill]), cap)
        if len(attempt) > MAX_CATALOG_CHARACTERS - 160:
            break
        kept.append(skill)
    remaining = len(visible) - len(kept)
    text = render(tuple(kept), cap)
    if remaining > 0:
        text += (
            f"\n（另有 {remaining} 个技能未列出：可用 /技能名 显式调用，"
            "或让用户说明任务后按需提供。）"
        )
    return text


class SkillService:
    def __init__(
        self,
        *,
        user_dir: Path,
        database_path: Path,
        override_repository: SqliteSkillOverrideRepository,
        extra_skill_dirs: Sequence[Path] = (),
        home_dir: Optional[Path] = None,
    ) -> None:
        self._user_dir = user_dir
        self._database_path = database_path
        self._override_repository = override_repository
        #: S4：除应用目录外的共享技能根（~/.claude/skills 等 + 环境变量追加）。
        self._extra_skill_dirs = tuple(extra_skill_dirs)
        #: 共享根使用的 home；显式传入优先，其次 `ENDLESS_TASK_SKILL_HOME`（测试隔离）。
        override = (os.environ.get("ENDLESS_TASK_SKILL_HOME") or "").strip()
        self._home_dir = (
            home_dir
            if home_dir is not None
            else (Path(override).expanduser() if override else None)
        )

    def root_specs(self, workspace_root: Optional[Path] = None) -> Tuple[SkillRootSpec, ...]:
        """S4：技能根清单（工作区 → 全局共享 → 应用自带）。"""
        return default_skill_root_specs(
            user_dir=self._user_dir,
            workspace_root=workspace_root,
            extra_dirs=self._extra_skill_dirs,
            home=self._home_dir,
        )

    def list_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        skills = discover_from_roots(self.root_specs(workspace_root))
        disabled = self._override_repository.disabled_index()
        user_disabled = disabled.get((SkillScope.USER.value, ""), set())
        workspace_disabled = disabled.get(
            (SkillScope.WORKSPACE.value, workspace_id), set()
        )
        return tuple(
            Skill(
                name=skill.name,
                description=skill.description,
                scope=skill.scope,
                file_path=skill.file_path,
                digest=skill.digest,
                version=skill.version,
                when_to_use=skill.when_to_use,
                source=skill.source,
                source_rank=skill.source_rank,
                disabled=skill.name in (
                    workspace_disabled if skill.scope == SkillScope.WORKSPACE else user_disabled
                ),
                disable_model_invocation=skill.disable_model_invocation,
                model_invocable=skill.model_invocable,
                user_invocable=skill.user_invocable,
                diagnostics=skill.diagnostics,
            )
            for skill in skills
        )

    def set_disabled(
        self,
        *,
        scope: SkillScope,
        name: str,
        disabled: bool,
        workspace_id: str = "",
    ) -> None:
        self._override_repository.set_disabled(
            scope=scope.value,
            workspace_id=workspace_id,
            name=name,
            disabled=disabled,
        )

    def visible_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        return tuple(
            skill
            for skill in self.list_skills(
                workspace_root, workspace_id=workspace_id
            )
            if skill.valid and not skill.disabled
        )

    def invocable_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        """S1：用户可显式调用的技能（与 model-invocable 解耦）。"""
        return tuple(
            skill
            for skill in self.visible_skills(workspace_root, workspace_id=workspace_id)
            if skill.user_invocable
        )

    def resolve_invocable(
        self,
        name: str,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Optional[Skill], str]:
        """解析显式调用：返回 (技能, 原因码)。

        名称根本不存在时返回 ``unknown``（普通文本里的 ``/tmp`` 不算错误）；
        存在但不允许用户调用/被禁用/清单非法时返回对应原因。
        """
        target = (name or "").strip()
        if not target:
            return None, INVOKE_UNKNOWN
        for skill in self.list_skills(workspace_root, workspace_id=workspace_id):
            if skill.name != target:
                continue
            if not skill.valid:
                return None, INVOKE_INVALID
            if skill.disabled:
                return None, INVOKE_DISABLED
            if not skill.user_invocable:
                return None, INVOKE_NOT_USER_INVOCABLE
            return skill, INVOKE_OK
        return None, INVOKE_UNKNOWN

    def skill_body(self, skill: Skill, *, max_bytes: int = MAX_SKILL_BODY_BYTES) -> str:
        """读取技能正文（去 frontmatter，有界，按字符安全截断）。"""
        try:
            text = skill.file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        # 注入给模型的是"正文"，不含 frontmatter（模型不需要看清单字段）。
        try:
            from .manifest import parse_skill_manifest

            parsed = parse_skill_manifest(skill.file_path, text)
            if parsed.body.strip():
                text = parsed.body
        except Exception:  # noqa: BLE001 解析失败时退回原文
            pass
        raw = text.encode("utf-8")
        if len(raw) <= max_bytes:
            return text
        # 按字符边界裁剪，避免切坏多字节字符。
        clipped = raw[:max_bytes]
        while clipped:
            try:
                return clipped.decode("utf-8") + "\n\n[技能正文已截断]"
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return ""

    def skill_roots(self, workspace_root: Optional[Path] = None) -> tuple[Path, ...]:
        """所有技能根（含共享目录）：读取正文时的根包含性校验用。"""
        return tuple(spec.path for spec in self.root_specs(workspace_root))
