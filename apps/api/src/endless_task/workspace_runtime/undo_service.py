"""A5 撤销/回滚服务：把可逆的文件副作用恢复到变更前状态。

设计要点：

* **只有可逆操作才有撤销**（文件写/删）；不可逆操作（外部动作、命令副作用）
  根本不进撤销日志，前端也就不提供入口；
* 撤销本身也是一次写操作：路径重新做 containment 校验（防越权）、结果写
  effect log（审计）、重复撤销幂等返回；
* 记录与撤销都 best-effort 且不吞掉工具结果：日志写失败只降级为"不可撤销"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from endless_task.storage import (
    FILE_DELETE,
    FILE_WRITE,
    SqliteUndoJournalRepository,
    UndoJournalEntry,
)
from endless_task.workspace_runtime.path_safety import resolve_workspace_path

logger = logging.getLogger(__name__)


class UndoUnavailableError(Exception):
    """该条目无法撤销（类型不支持 / 文件状态已变）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkspaceUndoService:
    """记录并执行工作区文件操作的撤销。"""

    def __init__(
        self,
        *,
        repository: SqliteUndoJournalRepository,
        effect_log=None,
    ) -> None:
        self._repository = repository
        self._effect_log = effect_log

    # ---------- 记录 ----------

    def record_file_write(
        self,
        *,
        conversation_id: str,
        target: str,
        workspace_root: str,
        before_content: Optional[str],
        before_exists: bool,
        after_hash: Optional[str] = None,
        workspace_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_execution_id: Optional[str] = None,
    ) -> Optional[UndoJournalEntry]:
        """记录一次文件写入（before_content=None 表示文件此前不存在）。"""
        action = "覆盖" if before_exists else "新建"
        return self._safe_append(
            conversation_id=conversation_id,
            kind=FILE_WRITE,
            target=target,
            workspace_root=workspace_root,
            before_exists=before_exists,
            before_content=before_content,
            after_hash=after_hash,
            description=f"{action}了文件 {target}",
            workspace_id=workspace_id,
            run_id=run_id,
            tool_execution_id=tool_execution_id,
        )

    def record_file_delete(
        self,
        *,
        conversation_id: str,
        target: str,
        workspace_root: str,
        before_content: str,
        workspace_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_execution_id: Optional[str] = None,
    ) -> Optional[UndoJournalEntry]:
        """记录一次文件删除（保留删除前内容以便恢复）。"""
        return self._safe_append(
            conversation_id=conversation_id,
            kind=FILE_DELETE,
            target=target,
            workspace_root=workspace_root,
            before_exists=True,
            before_content=before_content,
            description=f"删除了文件 {target}",
            workspace_id=workspace_id,
            run_id=run_id,
            tool_execution_id=tool_execution_id,
        )

    def _safe_append(self, **kwargs) -> Optional[UndoJournalEntry]:
        try:
            return self._repository.append(**kwargs)
        except Exception:  # noqa: BLE001 撤销日志失败不得影响工具结果
            logger.debug("Undo journal append failed", exc_info=True)
            return None

    # ---------- 撤销 ----------

    def undo(self, entry_id: str) -> tuple[UndoJournalEntry, bool]:
        """执行撤销；返回 (条目, 本次是否真的执行了撤销)。

        幂等：已撤销的条目再次调用直接返回，不会重复改文件。
        """
        entry = self._repository.get(entry_id)
        if not entry.undoable:
            return entry, False
        if entry.kind not in (FILE_WRITE, FILE_DELETE):
            raise UndoUnavailableError(
                "undo_not_supported", "这类操作不支持撤销，只能通过确认避免。"
            )
        resolved = resolve_workspace_path(Path(entry.workspace_root), entry.target)
        path = resolved.canonical
        if entry.kind == FILE_DELETE or entry.before_exists:
            content = entry.before_content
            if content is None:
                raise UndoUnavailableError(
                    "undo_snapshot_missing", "缺少变更前内容，无法撤销。"
                )
            if path.exists() and path.is_dir():
                raise UndoUnavailableError(
                    "undo_target_is_directory", "目标路径已是目录，无法恢复文件。"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        else:
            # 文件此前不存在 → 撤销 = 删除新建的文件。
            if path.exists() and path.is_file():
                path.unlink()
        undone = self._repository.mark_undone(entry_id)
        self._log_undo(entry)
        return undone, True

    def _log_undo(self, entry: UndoJournalEntry) -> None:
        log = self._effect_log
        if log is None:
            return
        try:
            from endless_task.workspace_runtime.effect_log import EffectReceipt

            log.append(
                conversation_id=entry.conversation_id,
                workspace_id=entry.workspace_id,
                workspace_root=entry.workspace_root,
                operation="undo",
                detail=entry.description,
                receipt=EffectReceipt(
                    kind=entry.kind,
                    path=entry.target,
                    executed_at="",
                ),
                approver="user",
            )
        except Exception:  # noqa: BLE001 审计失败不影响撤销结果
            logger.debug("Undo audit log failed", exc_info=True)


__all__ = ["UndoUnavailableError", "WorkspaceUndoService"]
