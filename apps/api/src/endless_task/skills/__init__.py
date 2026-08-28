"""R6.0 Skill 运行时：发现、校验、提示词拼装与状态服务。"""

from .models import Skill, SkillDiagnostic, SkillScope
from .loader import discover_skills
from .service import SkillService, build_available_skills_prompt

__all__ = [
    "Skill",
    "SkillDiagnostic",
    "SkillScope",
    "SkillService",
    "discover_skills",
    "build_available_skills_prompt",
]
