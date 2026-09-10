from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Tuple


class SkillScope(str, Enum):
    USER = "user"
    WORKSPACE = "workspace"


@dataclass(frozen=True)
class SkillDiagnostic:
    path: Path
    code: str
    message: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    scope: SkillScope
    file_path: Path
    #: 内容摘要与版本（SKILL.md 原始字节 sha256 / manifest version）。
    digest: str = ""
    version: str = ""
    #: S3：适用场景（人工可读；不进模型目录）。
    when_to_use: str = ""
    #: S4：技能来源标签（如 `~/.claude/skills` / `.endless-task/skills`）。
    source: str = ""
    #: 来源优先级（越大越优先，用于同名覆盖）。
    source_rank: int = 0
    #: S5：是否被用户固定进默认目录。
    pinned: bool = False
    disabled: bool = False
    disable_model_invocation: bool = False
    #: S1：调用策略（与模型调用解耦）。user_invocable 决定"用户 /技能名"是否可用。
    model_invocable: bool = True
    user_invocable: bool = True
    diagnostics: Tuple[SkillDiagnostic, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.diagnostics
