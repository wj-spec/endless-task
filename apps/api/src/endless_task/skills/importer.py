"""Local skill package import pipeline (M5 SK-4a).

07 §10 SK-4: first phase supports local directory import only. Import
copies the package to a staging directory, runs the static scanner, and
only then atomically activates it into the target skill root. No
Marketplace, no executable hooks.

Rules:

- the package must contain a parseable ``SKILL.md`` with a stable name;
- scanner findings at high/critical quarantine the import (never
  activated);
- an existing skill with the same name and a **different digest** is a
  conflict: activation requires ``allow_upgrade`` (explicit) and records
  the previous revision digest for rollback (SK-4b uses this to keep
  superseded revisions);
- activation is atomic: content is written to a staging path first, then
  renamed into place, so a crash never leaves a half-installed package.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .manifest import SkillManifest, parse_skill_manifest
from .registry import (
    InMemorySkillRegistry,
    SkillLocator,
    SkillRevision,
    SkillRoot,
)
from .scanner import (
    ScanReport,
    scan_skill_directory,
    scan_skill_revision,
)

#: Files that are never copied from a source package (defense in depth).
_FORBIDDEN_ENTRIES = frozenset({".git", ".svn", "__pycache__", ".DS_Store"})


@dataclass(frozen=True)
class ImportResult:
    name: str
    version: str
    digest: str
    target: Path
    replaced_digest: Optional[str] = None
    scan_report: Optional[ScanReport] = None

    @property
    def upgraded(self) -> bool:
        return self.replaced_digest is not None


@dataclass(frozen=True)
class ImportFailure(Exception):
    code: str
    message: str


def import_skill_package(
    source_dir: Path,
    target_root: Path,
    *,
    allow_upgrade: bool = False,
) -> ImportResult:
    """Import one local skill package into ``target_root`` (SK-4a)."""
    if not isinstance(source_dir, Path) or not source_dir.is_dir():
        raise ImportFailure("invalid_source", "技能来源必须是目录。")
    skill_file = source_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ImportFailure("missing_skill_file", "来源目录缺少 SKILL.md。")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ImportFailure("unreadable", "无法读取 SKILL.md。") from error
    manifest = parse_skill_manifest(skill_file, text)
    if not manifest.valid:
        codes = [diagnostic.code for diagnostic in manifest.diagnostics]
        raise ImportFailure(
            "invalid_manifest",
            f"SKILL.md manifest 无效：{', '.join(codes)}。",
        )
    if manifest.schema_version != 2:
        raise ImportFailure(
            "unsupported_schema",
            "导入要求 schema-version=2 的 manifest。",
        )
    if not manifest.name or "/" in manifest.name or "\\" in manifest.name:
        raise ImportFailure("invalid_name", "技能名无效。")

    revision_probe = SkillRevision.from_manifest(
        manifest,
        scope="user",
        path=skill_file,
    )
    body_scan = scan_skill_revision(revision_probe)
    directory_scan = scan_skill_directory(source_dir)
    scan = ScanReport(
        findings=body_scan.findings + directory_scan.findings
    )
    if scan.quarantined:
        worst = scan.worst_level.value if scan.worst_level is not None else "high"
        raise ImportFailure(
            "scanner_quarantined",
            f"技能未通过安全扫描（{worst}），导入被拒绝。",
        )

    target_package = target_root / manifest.name
    existing_digest: Optional[str] = None
    if target_package.exists():
        existing = _existing_digest(target_package)
        existing_digest = existing
        if existing is not None and existing != manifest.digest and not allow_upgrade:
            raise ImportFailure(
                "digest_conflict",
                (
                    f"技能 {manifest.name} 已存在且内容不同（digest "
                    f"{existing[:8]}… vs {manifest.digest[:8]}…）；"
                    "升级需显式 allow_upgrade。"
                ),
            )

    staging_root = Path(tempfile.mkdtemp(prefix="skill-import-"))
    try:
        staging_package = staging_root / manifest.name
        staging_package.mkdir(parents=True)
        _copy_package(source_dir, staging_package)
        # Atomic activation: the package is fully staged before it becomes
        # visible under the target root.
        target_package.parent.mkdir(parents=True, exist_ok=True)
        if target_package.exists():
            _replace_package(target_package, staging_package)
        else:
            shutil.move(str(staging_package), str(target_package))
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return ImportResult(
        name=manifest.name,
        version=manifest.version,
        digest=manifest.digest,
        target=target_package / "SKILL.md",
        replaced_digest=existing_digest,
        scan_report=scan,
    )


def _existing_digest(package_dir: Path) -> Optional[str]:
    skill_file = package_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    manifest = parse_skill_manifest(skill_file, text)
    return manifest.digest


def _copy_package(source_dir: Path, target_dir: Path) -> None:
    for entry in source_dir.iterdir():
        if entry.name in _FORBIDDEN_ENTRIES:
            continue
        destination = target_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        elif entry.is_file():
            shutil.copy2(entry, destination)


def _replace_package(target_dir: Path, staging_dir: Path) -> None:
    """Atomically replace an installed package with a staged one."""
    backup = target_dir.parent / f".{target_dir.name}.old"
    shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(target_dir), str(backup))
    try:
        shutil.move(str(staging_dir), str(target_dir))
        shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        # Roll the old package back so a failed upgrade never leaves the
        # skill missing.
        shutil.rmtree(target_dir, ignore_errors=True)
        shutil.move(str(backup), str(target_dir))
        raise


def verify_installed_revision(
    target_root: Path,
    locator: SkillLocator,
) -> Optional[Tuple[str, str]]:
    """Return (name, digest) of an installed skill, or None.

    Used by revision management (SK-4b) to correlate the on-disk digest
    with the registry state.
    """
    registry = InMemorySkillRegistry()
    registry.discover((SkillRoot(target_root, locator.scope, rank=0),))
    revision = registry.resolve(locator)
    if revision is None:
        return None
    return (revision.locator.name, revision.digest)


__all__ = [
    "ImportFailure",
    "ImportResult",
    "import_skill_package",
    "verify_installed_revision",
]
