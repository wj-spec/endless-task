"""副作用审计日志（EffectReceipt）：fs/shell 工具的确定性执行事实。

日志为本地 JSONL 追加写；工具成功即生成 receipt 进入结果，日志失败时
结果标记 unknown_outcome，绝不回传「未执行」。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from endless_task.tooling import ToolError

_LOG_LOCK = threading.Lock()
_MAX_LOG_LINES = 5_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class EffectReceipt:
    kind: str  # file_write | file_delete | shell
    path: str
    sha256: str = ""
    executed_at: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    truncated: bool = False
    unknown_outcome: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "executedAt": self.executed_at,
            "exitCode": self.exit_code,
            "timedOut": self.timed_out,
            "truncated": self.truncated,
            "unknownOutcome": self.unknown_outcome,
        }


class EffectLog:
    """本地副作用日志：JSONL 追加写，线程安全，按天分文件。"""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _log_path(self) -> Path:
        return self._log_dir / f"effects_{datetime.now(timezone.utc):%Y%m%d}.jsonl"

    def append(
        self,
        *,
        conversation_id: str,
        workspace_id: Optional[str],
        workspace_root: str,
        operation: str,
        detail: str,
        receipt: EffectReceipt,
        approver: str = "auto",
        duration_ms: int = 0,
    ) -> None:
        entry: dict[str, Any] = {
            "time": receipt.executed_at or _now_iso(),
            "conversationId": conversation_id,
            "workspaceId": workspace_id,
            "workspaceRoot": workspace_root,
            "operation": operation,
            "detail": detail,
            "receipt": receipt.as_dict(),
            "approver": approver,
            "durationMs": duration_ms,
        }
        raw = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        try:
            with _LOG_LOCK:
                path = self._log_path()
                exists = path.exists()
                with path.open("a", encoding="utf-8") as handle:
                    if not exists:
                        pass
                    handle.write(raw + "\n")
        except OSError as error:
            raise ToolError(
                "effect_log_failed",
                "副作用已发生，但本地审计日志写入失败；请到工作区设置检查日志目录。",
                retryable=False,
            ) from error

    def list_for_operation(
        self, prefix: str, *, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        """M2：按 operation 前缀读取最近条目（MCP 调用按 `mcp__` 前缀取）。"""
        entries: list[Mapping[str, Any]] = []
        paths = sorted(self._log_dir.glob("effects_*.jsonl"), reverse=True)
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("operation", "")).startswith(prefix):
                    entries.append(entry)
                    if len(entries) >= limit:
                        return entries
        return entries

    def list_for_workspace(
        self, workspace_id: str, *, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        entries: list[Mapping[str, Any]] = []
        paths = sorted(self._log_dir.glob("effects_*.jsonl"), reverse=True)
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("workspaceId") == workspace_id:
                    entries.append(entry)
                    if len(entries) >= limit:
                        return entries
        return entries


def trim_effect_log(log_dir: Path, *, keep_lines: int = _MAX_LOG_LINES) -> None:
    """当日日志超过行数上限时截断保留尾部（审计事实优先最近）。"""
    path = log_dir / f"effects_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= keep_lines:
        return
    try:
        with _LOG_LOCK:
            path.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    except OSError:
        pass
