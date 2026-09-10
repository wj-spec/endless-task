from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

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
        when_to_use=manifest.when_to_use,
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


#: 共享/全局技能根（按优先级从低到高）。
_SHARED_USER_ROOTS: Tuple[Tuple[str, int], ...] = (
    (".claude/skills", 10),
    (".codex/skills", 20),
    (".dsh/skills", 30),
    # `.agents` 视为本地 agent 之间最通用的约定，同名时优先级最高。
    (".agents/skills", 40),
)

#: 工作区级技能根（优先级高于所有全局根）。
_WORKSPACE_ROOTS: Tuple[Tuple[str, int], ...] = (
    (".claude/skills", 200),
    (".agents/skills", 210),
    (".dsh/skills", 220),
    (".endless-task/skills", 230),
)


@dataclass(frozen=True)
class SkillRootSpec:
    """一个技能根：路径 + 作用域 + 展示标签 + 优先级（越大越优先）。"""

    path: Path
    scope: SkillScope
    label: str
    rank: int


def default_skill_root_specs(
    *,
    user_dir: Path,
    workspace_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    home: Optional[Path] = None,
) -> Tuple[SkillRootSpec, ...]:
    """默认技能根清单：**工作区 → 全局共享 → 应用自带**（同名时高优先级胜出）。

    除应用自己的技能目录外，还识别其它本地 agent 的共享技能目录
    （`~/.claude/skills`、`~/.agents/skills`、`~/.dsh/skills`、`~/.codex/skills`），
    以及 `ENDLESS_TASK_SKILL_DIRS` 指定的额外目录。
    """
    base = home if home is not None else Path.home()
    specs: list[SkillRootSpec] = [
        SkillRootSpec(
            base / relative,
            SkillScope.USER,
            f"~/{relative}",
            rank,
        )
        for relative, rank in _SHARED_USER_ROOTS
    ]
    specs.append(SkillRootSpec(user_dir, SkillScope.USER, "应用技能目录", 100))
    for index, raw in enumerate(extra_dirs):
        path = Path(raw).expanduser()
        specs.append(
            SkillRootSpec(path, SkillScope.USER, str(path), 110 + index)
        )
    if workspace_root is not None:
        specs.extend(
            SkillRootSpec(
                workspace_root / relative,
                SkillScope.WORKSPACE,
                relative,
                rank,
            )
            for relative, rank in _WORKSPACE_ROOTS
        )
    return tuple(specs)


def discover_from_roots(specs: Iterable[SkillRootSpec]) -> Tuple[Skill, ...]:
    """按优先级合并多个技能根；同名技能由高优先级根覆盖。"""
    by_name: dict[str, Skill] = {}
    for spec in sorted(specs, key=lambda item: item.rank):
        for skill in _discover_in_root(spec.path, spec.scope):
            by_name[skill.name] = replace(
                skill, source=spec.label, source_rank=spec.rank
            )
    return tuple(sorted(by_name.values(), key=lambda s: s.name))


def discover_skills(user_dir: Path, workspace_dir: Optional[Path] = None) -> Tuple[Skill, ...]:
    """兼容入口：仅应用目录 + 单个工作区目录（旧签名，无共享根）。"""
    specs = [SkillRootSpec(user_dir, SkillScope.USER, "应用技能目录", 10)]
    if workspace_dir is not None:
        specs.append(
            SkillRootSpec(workspace_dir, SkillScope.WORKSPACE, "工作区技能", 20)
        )
    return discover_from_roots(specs)
