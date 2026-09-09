from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple
from xml.sax.saxutils import escape

from ..storage.sqlite_skill_override_repository import SqliteSkillOverrideRepository
from .loader import discover_skills
from .models import Skill, SkillScope

#: 单次注入的技能正文上限（字节）；超出按字符安全截断并标注。
MAX_SKILL_BODY_BYTES = 32 * 1024

#: 显式调用失败原因（供 API 与 UI 复用）。
INVOKE_OK = "ok"
INVOKE_UNKNOWN = "unknown"
INVOKE_NOT_USER_INVOCABLE = "not_user_invocable"
INVOKE_DISABLED = "disabled"
INVOKE_INVALID = "invalid"


def build_available_skills_prompt(
    skills: Tuple[Skill, ...],
    *,
    locator_mode: bool = False,
) -> str:
    visible = [skill for skill in skills if skill.valid and not skill.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "以下技能为特定任务提供专门指引。任务与某技能描述匹配时，先调用 read_skill_file "
        "读取其全文，再按其中步骤执行；技能内相对路径以其所在目录解析。",
        "<available_skills>",
    ]
    for skill in visible:
        location = (
            f"skill://{skill.scope.value}/{skill.name}"
            if locator_mode
            else str(skill.file_path)
        )
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                f"    <location>{escape(location)}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


class SkillService:
    def __init__(
        self,
        *,
        user_dir: Path,
        database_path: Path,
        override_repository: SqliteSkillOverrideRepository,
    ) -> None:
        self._user_dir = user_dir
        self._database_path = database_path
        self._override_repository = override_repository

    def list_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        workspace_dir = (
            workspace_root / ".endless-task" / "skills" if workspace_root else None
        )
        skills = discover_skills(self._user_dir, workspace_dir)
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
        roots = [self._user_dir]
        if workspace_root is not None:
            roots.append(workspace_root / ".endless-task" / "skills")
        return tuple(roots)
