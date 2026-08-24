from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

FILE_PREFIX = "file:"
MEMORY_PREFIX = "memory:"
MAX_SOURCE_LABELS = 20

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_RANGE_PATTERN = re.compile(r"^L(\d+)-L(\d+)$")


@dataclass(frozen=True)
class ParsedLabel:
    kind: str  # "file" | "memory" | "text"
    target_id: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


def parse_source_label(label: object) -> ParsedLabel:
    if not isinstance(label, str):
        return ParsedLabel(kind="text")
    raw = label.strip()
    if raw.startswith(FILE_PREFIX):
        body = raw[len(FILE_PREFIX):]
        file_id, _, range_part = body.partition(":")
        if not file_id or not _ID_PATTERN.fullmatch(file_id):
            return ParsedLabel(kind="text")
        if not range_part:
            return ParsedLabel(kind="file", target_id=file_id)
        range_match = _RANGE_PATTERN.fullmatch(range_part)
        if range_match is None:
            return ParsedLabel(kind="text")
        line_start = int(range_match.group(1))
        line_end = int(range_match.group(2))
        if line_start < 1 or line_end < line_start:
            return ParsedLabel(kind="text")
        return ParsedLabel(
            kind="file",
            target_id=file_id,
            line_start=line_start,
            line_end=line_end,
        )
    if raw.startswith(MEMORY_PREFIX):
        memory_id = raw[len(MEMORY_PREFIX):]
        if not memory_id or not _ID_PATTERN.fullmatch(memory_id):
            return ParsedLabel(kind="text")
        return ParsedLabel(kind="memory", target_id=memory_id)
    return ParsedLabel(kind="text")


def format_file_label(file_id: str) -> str:
    return f"{FILE_PREFIX}{file_id}"


def format_memory_label(memory_id: str) -> str:
    return f"{MEMORY_PREFIX}{memory_id}"
