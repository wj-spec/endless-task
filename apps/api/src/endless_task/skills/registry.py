"""Skill registry boundary and ``skill://`` locator (M5 SK-1a).

07 §6: skills are discovered from ordered roots (explicit workspace
selection > workspace > user > bundled) and addressed by
``skill://<scope>/<name>`` locators instead of host absolute paths. A
same name with a different digest across roots is a visible diagnostic,
never a silent last-file-wins choice.

SK-1a builds the registry boundary only: protocol + locator + revision
types + an in-memory registry over :mod:`.manifest` parsing. Wiring
``read_skill_file`` to locators and the feature flag land in SK-1b; the
production prompt still uses host paths until then.

SK-2b adds the lifecycle state: discovery auto-runs the static scanner,
grading each revision (07 §5.2). Quarantined or invalid revisions are
not resolvable (fail closed: they cannot be invoked through a locator),
while remaining visible in the discovery report for audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol, Tuple

from endless_task.agent_platform import require_identifier

from .manifest import SkillManifest, parse_skill_manifest
from .models import SkillDiagnostic
from .scanner import RiskLevel, ScanFinding, ScanReport, scan_skill_revision

#: ``skill://user/<name>`` | ``skill://workspace/<name>`` | ``skill://bundled/<name>``
_SCOPE_NAMES = ("user", "workspace", "bundled")
_SCOPE_RE = re.compile(r"^[a-z]+$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class SkillState(str, Enum):
    """Lifecycle state of a skill revision (07 §4.3)."""

    DISCOVERED = "discovered"
    VALIDATING = "validating"
    ACTIVE = "active"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"
    INVALID = "invalid"
    SUPERSEDED = "superseded"
    REMOVED = "removed"


#: States that must never be invocable through a locator (fail closed).
NON_INVOCABLE_STATES = frozenset(
    {
        SkillState.DISABLED,
        SkillState.QUARANTINED,
        SkillState.INVALID,
        SkillState.REMOVED,
        SkillState.SUPERSEDED,
    }
)


class SkillLocator:
    """Stable, host-independent address of one skill package."""

    __slots__ = ("scope", "name")

    def __init__(self, scope: str, name: str) -> None:
        self.scope = require_identifier(scope, field_name="skill_scope", max_length=32)
        self.name = require_identifier(name, field_name="skill_name", max_length=64)
        if self.scope not in _SCOPE_NAMES or not _SCOPE_RE.match(self.scope):
            raise ValueError(f"invalid skill scope: {self.scope!r}")
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid skill name: {self.name!r}")

    def __str__(self) -> str:
        return f"skill://{self.scope}/{self.name}"

    def __repr__(self) -> str:
        return f"SkillLocator({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SkillLocator) and str(self) == str(other)

    def __hash__(self) -> int:
        return hash(str(self))

    @classmethod
    def parse(cls, value: str) -> "SkillLocator":
        if not value.startswith("skill://"):
            raise ValueError(f"invalid skill locator: {value!r}")
        rest = value.removeprefix("skill://")
        parts = rest.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"invalid skill locator: {value!r}")
        return cls(scope=parts[0], name=parts[1])


@dataclass(frozen=True)
class SkillRoot:
    """One ordered discovery root with its source type."""

    path: Path
    source_type: str  # bundled | user | workspace
    rank: int  # lower wins: 0 explicit workspace, 1 workspace, 2 user, 3 bundled

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise ValueError("SkillRoot path must be a Path")
        if self.source_type not in _SCOPE_NAMES:
            raise ValueError(f"invalid skill root source: {self.source_type!r}")
        if not isinstance(self.rank, int) or self.rank < 0:
            raise ValueError("SkillRoot rank must be a non-negative integer")


@dataclass(frozen=True)
class SkillRevision:
    """One content-addressed revision of a skill package."""

    locator: SkillLocator
    version: str
    digest: str
    manifest_path: Path
    body: str
    model_invocable: bool = True
    user_invocable: bool = True
    required_tools: Tuple[str, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    diagnostics: Tuple[SkillDiagnostic, ...] = field(default_factory=tuple)
    state: SkillState = SkillState.DISCOVERED
    scan_findings: Tuple[ScanFinding, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.diagnostics

    @property
    def quarantined(self) -> bool:
        return self.state is SkillState.QUARANTINED

    @property
    def invocable(self) -> bool:
        return (
            self.state not in NON_INVOCABLE_STATES
            and self.valid
        )

    def scan_report(self) -> ScanReport:
        return ScanReport(findings=self.scan_findings)

    @classmethod
    def from_manifest(
        cls,
        manifest: SkillManifest,
        *,
        scope: str,
        path: Path,
    ) -> "SkillRevision":
        return cls(
            locator=SkillLocator(scope=scope, name=manifest.name),
            version=manifest.version,
            digest=manifest.digest,
            manifest_path=path,
            body=manifest.body,
            model_invocable=manifest.model_invocable,
            user_invocable=manifest.user_invocable,
            required_tools=manifest.required_tools,
            required_capabilities=manifest.required_capabilities,
            diagnostics=manifest.diagnostics,
        )


@dataclass(frozen=True)
class DiscoveryReport:
    """Outcome of discovering one root set."""

    revisions: Tuple[SkillRevision, ...]
    conflicts: Tuple[SkillDiagnostic, ...] = ()

    @property
    def by_locator(self) -> dict[SkillLocator, SkillRevision]:
        return {revision.locator: revision for revision in self.revisions}


class SkillRegistry(Protocol):
    def discover(self, roots: Tuple[SkillRoot, ...]) -> DiscoveryReport: ...

    def resolve(self, locator: SkillLocator) -> Optional[SkillRevision]: ...

    def visible_revisions(
        self,
        dependency_context: Optional[object] = None,
    ) -> Tuple[SkillRevision, ...]: ...


class InMemorySkillRegistry:
    """Registry resolving skill locators with explicit root precedence.

    Same-name revisions from different digests produce visible conflict
    diagnostics instead of a silent traversal-order winner.
    """

    def __init__(self) -> None:
        self._revisions: dict[SkillLocator, SkillRevision] = {}
        self._conflicts: list[SkillDiagnostic] = []

    def discover(self, roots: Tuple[SkillRoot, ...]) -> DiscoveryReport:
        ordered = tuple(sorted(roots, key=lambda root: root.rank))
        discovered: list[SkillRevision] = []
        conflicts: list[SkillDiagnostic] = []
        seen: dict[str, SkillRevision] = {}
        for root in ordered:
            for path in _skill_markdowns(root.path):
                manifest = parse_skill_manifest(path, _read_text(path))
                if manifest.diagnostics and not manifest.name:
                    continue
                revision = SkillRevision.from_manifest(
                    manifest,
                    scope=root.source_type,
                    path=path,
                )
                revision = _grade_revision(revision)
                key = revision.locator.name
                previous = seen.get(key)
                if previous is not None and previous.digest != revision.digest:
                    conflicts.append(
                        SkillDiagnostic(
                            path=path,
                            code="skill_digest_conflict",
                            message=(
                                f"技能 {key} 在多个来源中 digest 不同"
                                f"（{previous.digest[:8]}… vs {revision.digest[:8]}…）；"
                                "以更高优先级来源为准。"
                            ),
                        )
                    )
                if previous is None or root.rank <= _rank_of(previous, roots):
                    seen[key] = revision
        discovered = sorted(seen.values(), key=lambda item: str(item.locator))
        self._revisions = {revision.locator: revision for revision in discovered}
        self._conflicts = conflicts
        return DiscoveryReport(
            revisions=tuple(discovered),
            conflicts=tuple(conflicts),
        )

    def resolve(self, locator: SkillLocator) -> Optional[SkillRevision]:
        revision = self._revisions.get(locator)
        if revision is None or not revision.invocable:
            # Fail closed: quarantined/invalid/superseded/removed revisions
            # are not invocable through a locator (07 §5.3, M5 gate).
            return None
        return revision

    def set_state(self, locator: SkillLocator, state: SkillState) -> Optional[SkillRevision]:
        """Transition one revision's state (SK-4 rollback/disable support)."""
        revision = self._revisions.get(locator)
        if revision is None:
            return None
        updated = SkillRevision(
            locator=revision.locator,
            version=revision.version,
            digest=revision.digest,
            manifest_path=revision.manifest_path,
            body=revision.body,
            model_invocable=revision.model_invocable,
            user_invocable=revision.user_invocable,
            required_tools=revision.required_tools,
            required_capabilities=revision.required_capabilities,
            diagnostics=revision.diagnostics,
            state=state,
            scan_findings=revision.scan_findings,
        )
        self._revisions[locator] = updated
        return updated

    def visible_revisions(
        self,
        dependency_context: Optional[object] = None,
    ) -> Tuple[SkillRevision, ...]:
        """Revisions the model may see: active, model-invocable, deps met.

        ``dependency_context`` is a :class:`.dependencies.SkillDependencyContext`
        (imported lazily to keep registry free of a hard dependency);
        without one, only the state/model-invocable gates apply.
        """
        candidates = [
            revision
            for revision in self._revisions.values()
            if revision.state is SkillState.ACTIVE
            and revision.model_invocable
            and revision.invocable
        ]
        if dependency_context is None:
            return tuple(candidates)
        from .dependencies import dependency_satisfied_skills

        return dependency_satisfied_skills(tuple(candidates), dependency_context)

    @property
    def conflicts(self) -> Tuple[SkillDiagnostic, ...]:
        return tuple(self._conflicts)

    @property
    def revision_count(self) -> int:
        return len(self._revisions)


def _grade_revision(revision: SkillRevision) -> SkillRevision:
    """Assign lifecycle state from manifest validity and scanner findings.

    - manifest invalid -> INVALID (fail closed, never invocable),
    - scanner high/critical -> QUARANTINED,
    - otherwise ACTIVE.
    """
    if not revision.valid:
        return SkillRevision(
            locator=revision.locator,
            version=revision.version,
            digest=revision.digest,
            manifest_path=revision.manifest_path,
            body=revision.body,
            model_invocable=revision.model_invocable,
            user_invocable=revision.user_invocable,
            required_tools=revision.required_tools,
            required_capabilities=revision.required_capabilities,
            diagnostics=revision.diagnostics,
            state=SkillState.INVALID,
        )
    scan = scan_skill_revision(revision)
    if scan.quarantined:
        return SkillRevision(
            locator=revision.locator,
            version=revision.version,
            digest=revision.digest,
            manifest_path=revision.manifest_path,
            body=revision.body,
            model_invocable=revision.model_invocable,
            user_invocable=revision.user_invocable,
            required_tools=revision.required_tools,
            required_capabilities=revision.required_capabilities,
            diagnostics=revision.diagnostics,
            state=SkillState.QUARANTINED,
            scan_findings=scan.findings,
        )
    return SkillRevision(
        locator=revision.locator,
        version=revision.version,
        digest=revision.digest,
        manifest_path=revision.manifest_path,
        body=revision.body,
        model_invocable=revision.model_invocable,
        user_invocable=revision.user_invocable,
        required_tools=revision.required_tools,
        required_capabilities=revision.required_capabilities,
        diagnostics=revision.diagnostics,
        state=SkillState.ACTIVE,
        scan_findings=scan.findings,
    )


def _rank_of(revision: SkillRevision, roots: Tuple[SkillRoot, ...]) -> int:
    for root in roots:
        if root.source_type == revision.locator.scope:
            return root.rank
    return 99


def _skill_markdowns(root: Path) -> Tuple[Path, ...]:
    if not root.exists() or not root.is_dir():
        return ()
    found: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_dir():
            markdown = entry / "SKILL.md"
            if markdown.is_file():
                found.append(markdown)
            else:
                found.extend(_skill_markdowns(entry))
    return tuple(found)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


__all__ = [
    "DiscoveryReport",
    "NON_INVOCABLE_STATES",
    "SkillState",
    "InMemorySkillRegistry",
    "SkillLocator",
    "SkillRegistry",
    "SkillRevision",
    "SkillRoot",
]
