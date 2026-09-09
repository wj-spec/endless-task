from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from .manifest import parse_skill_manifest
from .models import Skill, SkillDiagnostic, SkillScope

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_NAME_FORBIDDEN = ("--",)
_MAX_DESCRIPTION_CHARACTERS = 1024


def _diagnostic(path: Path, code: str, message: str) -> SkillDiagnostic:
    return SkillDiagnostic(path=path, code=code, message=message)


def load_skill_file(path: Path, scope: SkillScope) -> Optional[Skill]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Skill(
            name=path.parent.name,
            description="",
            scope=scope,
            file_path=path,
            diagnostics=(_diagnostic(path, "unreadable", "无法读取技能文件。"),),
        )

    manifest = parse_skill_manifest(path, text)
    diagnostics: list[SkillDiagnostic] = list(manifest.diagnostics)
    if not manifest.name or not _NAME_RE.match(manifest.name) or any(
        marker in manifest.name for marker in _NAME_FORBIDDEN
    ):
        diagnostics.append(
            _diagnostic(path, "invalid_name", "技能名必须为小写字母、数字或连字符。")
        )
    if not manifest.description:
        diagnostics.append(
            _diagnostic(path, "missing_description", "缺少 description。")
        )
    elif len(manifest.description) > _MAX_DESCRIPTION_CHARACTERS:
        diagnostics.append(
            _diagnostic(
                path,
                "description_too_long",
                "描述超过 1024 字符。",
            )
        )
    if not manifest.body:
        diagnostics.append(
            _diagnostic(path, "empty_body", "技能正文不能为空。")
        )

    return Skill(
        name=manifest.name,
        description=manifest.description,
        scope=scope,
        file_path=path,
        digest=manifest.digest,
        version=manifest.version,
        disable_model_invocation=not manifest.model_invocable,
        model_invocable=manifest.model_invocable,
        user_invocable=manifest.user_invocable,
        diagnostics=tuple(diagnostics),
    )


def _discover_in_root(root: Path, scope: SkillScope) -> Tuple[Skill, ...]:
    if not root.exists() or not root.is_dir():
        return ()
    skills: list[Skill] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return ()
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            skill = load_skill_file(entry / "SKILL.md", scope)
            if skill is not None:
                skills.append(skill)
            continue
        if entry.is_dir():
            skills.extend(_discover_in_root(entry, scope))
    return tuple(skills)


def discover_skills(user_dir: Path, workspace_dir: Optional[Path] = None) -> Tuple[Skill, ...]:
    """发现用户级与工作区级技能；工作区同名技能覆盖用户级。"""
    skills: dict[str, Skill] = {}
    for skill in _discover_in_root(user_dir, SkillScope.USER):
        skills[skill.name] = skill
    if workspace_dir is not None:
        for skill in _discover_in_root(workspace_dir, SkillScope.WORKSPACE):
            skills[skill.name] = skill
    return tuple(sorted(skills.values(), key=lambda s: s.name))
