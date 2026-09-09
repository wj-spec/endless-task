"""R6.0 Skill 运行时：发现、校验、提示词拼装与状态服务。"""

from .importer import ImportFailure, ImportResult, import_skill_package
from .manifest import SkillManifest, parse_skill_manifest
from .models import Skill, SkillDiagnostic, SkillScope
from .dependencies import (
    DependencyReport,
    SkillDependencyContext,
    check_skill_dependencies,
    dependency_satisfied_skills,
)
from .invocation import (
    MAX_SKILL_REQUESTS,
    SKILL_COMMAND_RE,
    parse_skill_commands,
)
from .loader import discover_skills
from .scanner import (
    QUARANTINE_LEVELS,
    RiskLevel,
    ScanFinding,
    ScanReport,
    ScannerRuleSet,
    scan_skill_directory,
    scan_skill_revision,
)
from .package_cases import (
    PackageCase,
    PackageCaseResult,
    PackageCaseSuite,
    load_package_cases,
    run_package_case,
    run_package_suite,
)
from .usage import (
    InMemorySkillUsageRecorder,
    SkillUsageEvent,
    SkillUsageSnapshot,
)
from .registry import (
    DiscoveryReport,
    NON_INVOCABLE_STATES,
    InMemorySkillRegistry,
    SkillLocator,
    SkillRevision,
    SkillRoot,
    SkillState,
)
from .service import (
    INVOKE_DISABLED,
    INVOKE_INVALID,
    INVOKE_NOT_USER_INVOCABLE,
    INVOKE_OK,
    INVOKE_UNKNOWN,
    MAX_SKILL_BODY_BYTES,
    SkillService,
    build_available_skills_prompt,
)

__all__ = [
    "INVOKE_DISABLED",
    "INVOKE_INVALID",
    "INVOKE_NOT_USER_INVOCABLE",
    "INVOKE_OK",
    "INVOKE_UNKNOWN",
    "MAX_SKILL_BODY_BYTES",
    "MAX_SKILL_REQUESTS",
    "SKILL_COMMAND_RE",
    "parse_skill_commands",
    "Skill",
    "SkillDiagnostic",
    "SkillScope",
    "DependencyReport",
    "SkillDependencyContext",
    "check_skill_dependencies",
    "dependency_satisfied_skills",
    "SkillService",
    "discover_skills",
    "build_available_skills_prompt",
    "ImportFailure",
    "PackageCase",
    "PackageCaseResult",
    "PackageCaseSuite",
    "load_package_cases",
    "run_package_case",
    "run_package_suite",
    "InMemorySkillUsageRecorder",
    "SkillUsageEvent",
    "SkillUsageSnapshot",
    "ImportResult",
    "import_skill_package",
    "SkillManifest",
    "parse_skill_manifest",
    "DiscoveryReport",
    "InMemorySkillRegistry",
    "SkillLocator",
    "SkillRevision",
    "SkillRoot",
    "SkillState",
    "NON_INVOCABLE_STATES",
    "QUARANTINE_LEVELS",
    "RiskLevel",
    "ScanFinding",
    "ScanReport",
    "ScannerRuleSet",
    "scan_skill_directory",
    "scan_skill_revision",
]
