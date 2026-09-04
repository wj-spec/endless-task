from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple
from xml.sax.saxutils import escape

from ..storage.sqlite_skill_override_repository import SqliteSkillOverrideRepository
from .loader import discover_skills
from .models import Skill, SkillScope


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
                disabled=skill.name in (
                    workspace_disabled if skill.scope == SkillScope.WORKSPACE else user_disabled
                ),
                disable_model_invocation=skill.disable_model_invocation,
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

    def skill_roots(self, workspace_root: Optional[Path] = None) -> tuple[Path, ...]:
        roots = [self._user_dir]
        if workspace_root is not None:
            roots.append(workspace_root / ".endless-task" / "skills")
        return tuple(roots)
