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
    disabled: bool = False
    disable_model_invocation: bool = False
    diagnostics: Tuple[SkillDiagnostic, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.diagnostics
