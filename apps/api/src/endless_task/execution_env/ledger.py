"""Agent file mutation ledger (M3B AP-305).

Append-only JSONL ledger recording file mutations with before/after content
hashes so restore/undo can tell agent changes from later user changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError, require_identifier


@dataclass(frozen=True)
class LedgerEntry:
    effect_id: str
    path: str
    operation: str
    timestamp: str
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None

    def to_json(self) -> Mapping[str, Any]:
        return {
            "effect_id": self.effect_id,
            "path": self.path,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }


class FileMutationLedger:
    """Append-only file mutation ledger (JSONL, one entry per line)."""

    def __init__(self, ledger_path: Path) -> None:
        self._path = Path(ledger_path)

    def append(self, entry: LedgerEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        entry.to_json(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        except OSError as error:
            raise AgentPlatformError(
                "ledger_write_failed",
                "文件变更账本写入失败。",
                retryable=False,
                details={"error_type": type(error).__name__},
            ) from error

    def entries(self) -> tuple[LedgerEntry, ...]:
        if not self._path.exists():
            return ()
        rows: list[LedgerEntry] = []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise AgentPlatformError(
                "ledger_read_failed",
                "文件变更账本读取失败。",
                retryable=False,
                details={"error_type": type(error).__name__},
            ) from error
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError as error:
                raise AgentPlatformError(
                    "ledger_corrupt",
                    "文件变更账本包含损坏条目。",
                    retryable=False,
                ) from error
            rows.append(
                LedgerEntry(
                    effect_id=require_identifier(
                        raw.get("effect_id", ""), field_name="effect_id"
                    ),
                    path=require_identifier(raw.get("path", ""), field_name="path"),
                    operation=require_identifier(
                        raw.get("operation", ""), field_name="operation"
                    ),
                    timestamp=require_identifier(
                        raw.get("timestamp", ""), field_name="timestamp"
                    ),
                    before_hash=raw.get("before_hash"),
                    after_hash=raw.get("after_hash"),
                )
            )
        return tuple(rows)

    def last_entry_for(self, path: str) -> Optional[LedgerEntry]:
        normalized = require_identifier(path, field_name="path")
        for entry in reversed(self.entries()):
            if entry.path == normalized:
                return entry
        return None


def ledger_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["FileMutationLedger", "LedgerEntry", "ledger_timestamp"]
