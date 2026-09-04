"""Static security scanner for skill packages (M5 SK-2a).

07 §5: a skill body is untrusted content. The scanner runs static rules
over body, resources, symlinks and provenance and grades findings:

- ``info``     allowed, recorded diagnostic
- ``warning``  active by default but visible to the user
- ``high``     quarantine, explicit review required
- ``critical`` activation forbidden

Scanning is NOT the tool-execution security boundary; it decides whether
a skill may activate. Bundled skills are scanned too (release process
audits them; bundled status never skips rules).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from .registry import SkillRevision


class RiskLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


#: Levels that force quarantine (07 §5.2).
QUARANTINE_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})


@dataclass(frozen=True)
class ScanFinding:
    code: str
    level: RiskLevel
    message: str
    match: Optional[str] = None
    path: Optional[str] = None

    def to_json(self) -> dict:
        payload: dict = {
            "code": self.code,
            "level": self.level.value,
            "message": self.message,
        }
        if self.match is not None:
            payload["match"] = self.match
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class ScanReport:
    findings: Tuple[ScanFinding, ...] = ()

    @property
    def worst_level(self) -> Optional[RiskLevel]:
        order = (
            RiskLevel.INFO,
            RiskLevel.WARNING,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        )
        worst: Optional[RiskLevel] = None
        for finding in self.findings:
            if worst is None or order.index(finding.level) > order.index(worst):
                worst = finding.level
        return worst

    @property
    def quarantined(self) -> bool:
        return any(finding.level in QUARANTINE_LEVELS for finding in self.findings)

    @property
    def critical(self) -> bool:
        return any(finding.level is RiskLevel.CRITICAL for finding in self.findings)


#: Line-oriented pattern rules over the skill body. Each entry is
#: (code, level, regex, message). Rules are declarative data so they can be
#: versioned and reviewed without touching scanner code (README 4.7).
_BODY_RULES: tuple[tuple[str, RiskLevel, str, str], ...] = (
    (
        "prompt_role_override",
        RiskLevel.HIGH,
        r"(?i)\b(system prompt|you are now|ignore (previous|all prior) instructions|"
        r"disregard (your|all) (instructions|rules)|jailbreak)\b",
        "疑似提示注入或角色覆盖。",
    ),
    (
        "credential_read",
        RiskLevel.HIGH,
        r"(?i)\b(~?/?\.ssh|id_rsa|id_ed25519|\.aws/credentials|keychain|"
        r"browser profile|login\.keychain|credentials\.json)\b",
        "疑似读取凭据/密钥/浏览器 profile。",
    ),
    (
        "download_then_execute",
        RiskLevel.HIGH,
        r"(?i)\b(curl|wget|powershell|Invoke-WebRequest).{0,120}?\b(;|\||&&|\n)\s*"
        r"(sh|bash|python|node|perl)\b",
        "疑似下载后执行。",
    ),
    (
        "obfuscated_execution",
        RiskLevel.HIGH,
        r"(?i)\b(base64|hex)\b.{0,60}\b(-d|--decode|decode)\b",
        "疑似解码后执行（base64/hex）。",
    ),
    (
        "destructive_commands",
        RiskLevel.CRITICAL,
        r"(?i)(rm\s+-rf\s+/|mkfs\.|format\s+[a-z]:|diskutil\s+eraseDisk|"
        r"chmod\s+-R\s+777\s+/)",
        "破坏性删除/格式化/权限放开。",
    ),
    (
        "persistence_or_reverse_shell",
        RiskLevel.CRITICAL,
        r"(?i)\b(crontab|launchctl\s+load|/etc/rc\.local|nohup.{0,40}"
        r"(bash|sh|python).{0,40}&|reverse shell|nc\s+-e|/dev/tcp/)\b",
        "疑似持久化或反向 Shell。",
    ),
    (
        "opaque_network_upload",
        RiskLevel.WARNING,
        r"(?i)\b(curl|wget|Invoke-WebRequest|requests\.(post|put)|urllib).{0,120}?"
        r"(-d|--data|--upload-file|data=|files=)\b",
        "外部数据上传；目标网络不透明时需复核。",
    ),
    (
        "eval_unsafe",
        RiskLevel.WARNING,
        r"(?i)\b(eval\s*\(|exec\s*\(|child_process|os\.system|subprocess\.call|"
        r"subprocess\.Popen|Process\.Start)\b",
        "执行任意命令/代码；风险由执行侧 Tool 审批兜底。",
    ),
    (
        "known_credential_pattern",
        RiskLevel.HIGH,
        r"(?i)\b(api[\s_-]*key|secret[\s_-]*key|access[\s_-]*token|password|passwd)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]",
        "疑似内嵌密钥。",
    ),
    (
        "path_traversal",
        RiskLevel.HIGH,
        r"(?i)\b(\.\./){2,}|\\\\\.\.|%2e%2e|\.\.[/\\\\]",
        "疑似路径穿越。",
    ),
)


@dataclass(frozen=True)
class ScannerRuleSet:
    """Data-driven rule set; defaults mirror 07 §5.1 categories."""

    body_rules: Tuple[tuple[str, RiskLevel, str, str], ...] = _BODY_RULES
    max_body_characters: int = 200_000
    max_resource_bytes: int = 5_000_000
    max_control_characters: int = 16
    allow_symlinks: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.max_body_characters, int) or self.max_body_characters <= 0:
            raise ValueError("max_body_characters must be positive")
        if not isinstance(self.max_resource_bytes, int) or self.max_resource_bytes <= 0:
            raise ValueError("max_resource_bytes must be positive")


def scan_skill_revision(
    revision: SkillRevision,
    *,
    rule_set: Optional[ScannerRuleSet] = None,
) -> ScanReport:
    """Run static rules over one skill revision (body + package directory)."""
    rules = rule_set if rule_set is not None else ScannerRuleSet()
    findings: list[ScanFinding] = []

    body = revision.body or ""
    if len(body) > rules.max_body_characters:
        findings.append(
            ScanFinding(
                code="body_too_large",
                level=RiskLevel.WARNING,
                message="技能正文超过大小上限。",
            )
        )
    control = _control_character_count(body)
    if control > rules.max_control_characters:
        findings.append(
            ScanFinding(
                code="unusual_control_characters",
                level=RiskLevel.WARNING,
                message="正文包含异常多的控制字符。",
            )
        )
    for code, level, pattern, message in rules.body_rules:
        match = _first_match(body, pattern)
        if match is not None:
            findings.append(
                ScanFinding(
                    code=code,
                    level=level,
                    message=message,
                    match=_clip(match),
                )
            )

    findings.extend(_scan_package_directory(revision.manifest_path.parent, rules))
    return ScanReport(findings=tuple(findings))


def scan_skill_directory(
    package_dir: Path,
    *,
    rule_set: Optional[ScannerRuleSet] = None,
) -> ScanReport:
    """Scan a skill package directory (used by SK-4 import staging)."""
    rules = rule_set if rule_set is not None else ScannerRuleSet()
    findings: list[ScanFinding] = []
    findings.extend(_scan_package_directory(package_dir, rules))
    return ScanReport(findings=tuple(findings))


def _scan_package_directory(
    package_dir: Path,
    rules: ScannerRuleSet,
) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    if not package_dir.exists() or not package_dir.is_dir():
        return findings
    for entry in sorted(package_dir.rglob("*"), key=lambda p: str(p)):
        if entry.is_symlink():
            target = entry.resolve(strict=False)
            inside = _is_within(package_dir.resolve(), target)
            if not inside:
                findings.append(
                    ScanFinding(
                        code="symlink_escape",
                        level=RiskLevel.CRITICAL,
                        message="符号链接指向技能包目录之外。",
                        path=str(entry),
                    )
                )
            elif not rules.allow_symlinks:
                findings.append(
                    ScanFinding(
                        code="symlink_present",
                        level=RiskLevel.WARNING,
                        message="技能包含符号链接；默认不允许。",
                        path=str(entry),
                    )
                )
            continue
        if entry.is_file():
            size = _safe_size(entry)
            if size > rules.max_resource_bytes:
                findings.append(
                    ScanFinding(
                        code="resource_too_large",
                        level=RiskLevel.WARNING,
                        message="技能资源超过大小上限。",
                        path=str(entry),
                    )
                )
    return findings


def _first_match(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text)
    return match.group(0) if match is not None else None


def _clip(value: str, limit: int = 80) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _control_character_count(text: str) -> int:
    return sum(1 for character in text if ord(character) < 32 and character not in "\n\r\t")


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "QUARANTINE_LEVELS",
    "RiskLevel",
    "ScanFinding",
    "ScanReport",
    "ScannerRuleSet",
    "scan_skill_directory",
    "scan_skill_revision",
]
