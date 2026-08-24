from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from endless_task.domain.models import MemoryStatus
from endless_task.domain.repositories import RepositoryError
from endless_task.files import FileError

from .source_labels import parse_source_label

MEMORY_SNIPPET_CHARS = 80


@dataclass(frozen=True)
class SourceReference:
    label: str
    type: str  # "file" | "memory" | "text"
    resolved: bool
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    line_range: Optional[tuple[int, int]] = None
    memory_id: Optional[str] = None
    memory_snippet: Optional[str] = None


class SourceReferenceResolver:
    """Resolves stored source labels into display-ready references.

    Failures degrade to ``resolved=False`` references; rendering never raises.
    """

    def __init__(self, *, file_repository=None, memory_repository=None) -> None:
        self._file_repository = file_repository
        self._memory_repository = memory_repository

    def resolve(
        self,
        labels: Sequence[str],
        *,
        conversation_id: str,
    ) -> tuple[SourceReference, ...]:
        references: list[SourceReference] = []
        for label in labels:
            parsed = parse_source_label(label)
            if parsed.kind == "file":
                references.append(
                    self._resolve_file(str(label), parsed, conversation_id)
                )
            elif parsed.kind == "memory":
                references.append(self._resolve_memory(str(label), parsed))
            else:
                references.append(
                    SourceReference(label=str(label), type="text", resolved=False)
                )
        return tuple(references)

    def _resolve_file(self, label, parsed, conversation_id) -> SourceReference:
        line_range = (
            (parsed.line_start, parsed.line_end)
            if parsed.line_start is not None and parsed.line_end is not None
            else None
        )
        if self._file_repository is None:
            return SourceReference(
                label=label,
                type="file",
                resolved=False,
                file_id=parsed.target_id,
                line_range=line_range,
            )
        try:
            stored = self._file_repository.get_file(
                conversation_id=conversation_id,
                file_id=parsed.target_id,
            )
        except FileError:
            return SourceReference(
                label=label,
                type="file",
                resolved=False,
                file_id=parsed.target_id,
                line_range=line_range,
            )
        return SourceReference(
            label=label,
            type="file",
            resolved=True,
            file_id=parsed.target_id,
            file_name=stored.metadata.original_name,
            line_range=line_range,
        )

    def _resolve_memory(self, label, parsed) -> SourceReference:
        if self._memory_repository is None:
            return SourceReference(
                label=label,
                type="memory",
                resolved=False,
                memory_id=parsed.target_id,
            )
        try:
            record = self._memory_repository.get_memory(parsed.target_id)
        except RepositoryError:
            return SourceReference(
                label=label,
                type="memory",
                resolved=False,
                memory_id=parsed.target_id,
            )
        if record.status is not MemoryStatus.ACTIVE:
            return SourceReference(
                label=label,
                type="memory",
                resolved=False,
                memory_id=parsed.target_id,
            )
        snippet = record.content.strip().replace("\n", " ")[:MEMORY_SNIPPET_CHARS]
        return SourceReference(
            label=label,
            type="memory",
            resolved=True,
            memory_id=parsed.target_id,
            memory_snippet=snippet,
        )
