"""Skill usage recording (M5 SK-5).

07 §8.2: usage data is recorded per revision so upgrade-vs-old behavior is
never conflated. Counters the runtime can feed:

- ``surfaced`` — the revision was offered to the model,
- ``invoked`` — explicitly invoked (user or model),
- ``body_read`` — the SKILL.md body was read,
- ``missing_dependencies`` — a required tool/capability was absent when
  the skill was considered.

This module is an in-memory recorder (composition root wires persistence
later); it stays vocabulary-neutral and keyed by content digest.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class SkillUsageSnapshot:
    surfaced: int = 0
    invoked: int = 0
    body_read: int = 0
    missing_dependencies: int = 0

    def __add__(self, other: "SkillUsageSnapshot") -> "SkillUsageSnapshot":
        return SkillUsageSnapshot(
            surfaced=self.surfaced + other.surfaced,
            invoked=self.invoked + other.invoked,
            body_read=self.body_read + other.body_read,
            missing_dependencies=self.missing_dependencies + other.missing_dependencies,
        )


@dataclass(frozen=True)
class SkillUsageEvent:
    digest: str
    kind: str  # surfaced | invoked | body_read | missing_dependencies


class InMemorySkillUsageRecorder:
    """Thread-safe per-digest usage counters."""

    def __init__(self) -> None:
        self._counts: Dict[str, SkillUsageSnapshot] = {}
        self._lock = threading.Lock()
        self._events: list[SkillUsageEvent] = []

    def record(self, event: SkillUsageEvent) -> None:
        with self._lock:
            current = self._counts.get(event.digest, SkillUsageSnapshot())
            if event.kind == "surfaced":
                updated = SkillUsageSnapshot(
                    surfaced=current.surfaced + 1,
                    invoked=current.invoked,
                    body_read=current.body_read,
                    missing_dependencies=current.missing_dependencies,
                )
            elif event.kind == "invoked":
                updated = SkillUsageSnapshot(
                    surfaced=current.surfaced,
                    invoked=current.invoked + 1,
                    body_read=current.body_read,
                    missing_dependencies=current.missing_dependencies,
                )
            elif event.kind == "body_read":
                updated = SkillUsageSnapshot(
                    surfaced=current.surfaced,
                    invoked=current.invoked,
                    body_read=current.body_read + 1,
                    missing_dependencies=current.missing_dependencies,
                )
            elif event.kind == "missing_dependencies":
                updated = SkillUsageSnapshot(
                    surfaced=current.surfaced,
                    invoked=current.invoked,
                    body_read=current.body_read,
                    missing_dependencies=current.missing_dependencies + 1,
                )
            else:
                raise ValueError(f"unknown usage kind: {event.kind!r}")
            self._counts[event.digest] = updated
            self._events.append(event)

    def surfaced(self, digest: str) -> None:
        self.record(SkillUsageEvent(digest=digest, kind="surfaced"))

    def invoked(self, digest: str) -> None:
        self.record(SkillUsageEvent(digest=digest, kind="invoked"))

    def body_read(self, digest: str) -> None:
        self.record(SkillUsageEvent(digest=digest, kind="body_read"))

    def missing_dependencies(self, digest: str) -> None:
        self.record(SkillUsageEvent(digest=digest, kind="missing_dependencies"))

    def snapshot(self, digest: str) -> SkillUsageSnapshot:
        with self._lock:
            return self._counts.get(digest, SkillUsageSnapshot())

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)


__all__ = [
    "InMemorySkillUsageRecorder",
    "SkillUsageEvent",
    "SkillUsageSnapshot",
]
