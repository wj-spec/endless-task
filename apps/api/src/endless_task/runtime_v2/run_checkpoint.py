"""Per-run workspace checkpoint + restore coordinator (M3B run-level, A).

05 AP-305 的 checkpoint/restore 原语已存在，但无 run 级消费（核对表
「run 级恢复未接入」）。本模块按方案 A 落地真实恢复能力：

- **run 首次写副作用前自动 checkpoint 工作区**：coordinator 记一次
  ``ensure_checkpoint(run_id, workspace_root)`` —— 同一 run 对同一
  workspace 只 checkpoint 一次（快照成本一次），后续 restore 以此为准。
- **restore 守卫**：``plan_restore``/``apply_restore`` 复用 05 语义——
  文件当前内容 ≠ checkpoint 时，仅当账本最近一条 agent 记录 after_hash 与
  当前内容一致才判定为「agent 改动未被动过」→ 恢复；内容在 agent 写入后
  又被改动（用户编辑、其它 run、任何非受管改动）→ 判为用户修改跳过；
  checkpoint 后新建文件不动（绝不覆盖用户修改，05 退出门）。
- **每 run 单 checkpoint**（05 line 517 记录），conversation 内多次 run
  各自独立 checkpoint id。

coordinator 是**有状态协调层**，把「何时 checkpoint / 何时 restore / 从哪
读账本」从执行引擎解耦；写工具/executor 只调 ensure/restore 两个方法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from endless_task.agent_platform import AgentPlatformError, require_identifier
from endless_task.execution_env.checkpoint import (
    CheckpointManifest,
    apply_restore,
    create_checkpoint,
    read_manifest,
)
from endless_task.execution_env.ledger import (
    FileMutationLedger,
    LedgerEntry,
    ledger_timestamp,
)

logger = logging.getLogger(__name__)

#: dirs never snapshotted (checkpoint store itself, hidden caches).
_DEFAULT_SKIP_PREFIXES = (
    ".endless-task-checkpoints",
    ".endless-task/checkpoints",
    ".git",
    "node_modules",
)


@dataclass(frozen=True)
class RunCheckpointRef:
    run_id: str
    workspace_root: str
    checkpoint_id: str
    created_at: str


@dataclass(frozen=True)
class RestoreRunOutcome:
    run_id: str
    workspace_root: str
    restored: tuple[str, ...]
    skipped: tuple[str, ...]


class RunCheckpointCoordinator:
    """Per-run workspace checkpoint/restore coordination (run-level A)."""

    def __init__(
        self,
        *,
        store_root: Path,
        ledger: Optional[FileMutationLedger] = None,
        skip_prefixes: tuple[str, ...] = _DEFAULT_SKIP_PREFIXES,
    ) -> None:
        self._store_root = Path(store_root).expanduser().resolve(strict=False)
        self._store_root.mkdir(parents=True, exist_ok=True)
        self._ledger = ledger
        self._skip_prefixes = tuple(skip_prefixes)
        # run_id -> (workspace_root, RunCheckpointRef); one checkpoint per
        # (run, workspace).
        self._checkpoints: dict[str, dict[str, RunCheckpointRef]] = {}

    # -- checkpoint --------------------------------------------------------

    def ensure_checkpoint(
        self,
        *,
        run_id: str,
        workspace_root: str,
        trace_id: str = "",
    ) -> RunCheckpointRef:
        """Snapshot a workspace for a run exactly once (per run+workspace).

        Called before the run's first write side effect; idempotent for the
        same run+workspace so repeated writes never re-snapshot.
        """
        normalized_run = require_identifier(run_id, field_name="run_id")
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise AgentPlatformError(
                "invalid_checkpoint_root",
                f"工作区根目录不可用，无法 checkpoint：{workspace_root}",
                retryable=False,
            )
        by_workspace = self._checkpoints.setdefault(normalized_run, {})
        canonical_root = str(root)
        existing = by_workspace.get(canonical_root)
        if existing is not None:
            return existing

        checkpoint_id = require_identifier(
            f"{normalized_run}:{len(by_workspace)}",
            field_name="checkpoint_id",
            max_length=256,
        )
        manifest = create_checkpoint(
            root,
            self._store_root,
            checkpoint_id=checkpoint_id,
            skip_prefixes=self._skip_prefixes,
        )
        ref = RunCheckpointRef(
            run_id=normalized_run,
            workspace_root=canonical_root,
            checkpoint_id=manifest.checkpoint_id,
            created_at=ledger_timestamp(),
        )
        by_workspace[canonical_root] = ref
        logger.info(
            "Run workspace checkpoint created",
            extra={"run_id": run_id, "workspace": canonical_root},
        )
        return ref

    # -- restore -----------------------------------------------------------

    def plan_restore(self, *, run_id: str, workspace_root: str):
        """Dry-run plan for one run+workspace checkpoint (never mutates)."""
        ref = self._require_ref(run_id, workspace_root)
        manifest = self._load_manifest(ref)
        from endless_task.execution_env.checkpoint import plan_restore as _plan

        return _plan(manifest, Path(ref.workspace_root), ledger=self._ledger)

    def apply_restore(
        self,
        *,
        run_id: str,
        workspace_root: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Restore agent changes to the run checkpoint; user changes kept."""
        ref = self._require_ref(run_id, workspace_root)
        manifest = self._load_manifest(ref)
        restored, skipped = apply_restore(
            manifest,
            Path(ref.workspace_root),
            self._store_root,
            ledger=self._ledger,
        )
        logger.info(
            "Run workspace restored from checkpoint",
            extra={
                "run_id": run_id,
                "workspace": workspace_root,
                "restored": len(restored),
                "skipped": len(skipped),
            },
        )
        return restored, skipped

    def workspace_roots_for(self, *, run_id: str) -> tuple[str, ...]:
        """Workspace roots checkpointed for a run (ordered)."""
        return tuple(self._checkpoints.get(run_id, {}))

    def restore_run(self, *, run_id: str) -> tuple[RestoreRunOutcome, ...]:
        """Restore every workspace this run checkpointed (M3B slice B).

        Each workspace uses the guarded apply_restore (never overwrites user
        edits, never touches post-checkpoint files). Runs without checkpoints
        return no outcomes. Applying twice is a no-op: after the first restore
        current content equals the checkpoint.
        """
        outcomes: list[RestoreRunOutcome] = []
        for workspace_root in self.workspace_roots_for(run_id=run_id):
            restored, skipped = self.apply_restore(
                run_id=run_id, workspace_root=workspace_root
            )
            outcomes.append(
                RestoreRunOutcome(
                    run_id=run_id,
                    workspace_root=workspace_root,
                    restored=restored,
                    skipped=skipped,
                )
            )
        return tuple(outcomes)

    def record_effect(
        self,
        *,
        effect_id: str,
        path: str,
        operation: str,
        before_hash: Optional[str] = None,
        after_hash: Optional[str] = None,
    ) -> None:
        """Ledger an agent file side effect (interactive tool path, M3B A).

        Execution-env backends append their own ledger rows; workspace tools
        (write/delete) reach this hook so the run coordinator sees the same
        attribution. Best-effort: a failed append only degrades restore
        attribution (the change is then treated as user-owned and skipped),
        never blocks the tool result.
        """
        if self._ledger is None:
            return
        try:
            self._ledger.append(
                LedgerEntry(
                    effect_id=effect_id,
                    path=path,
                    operation=operation,
                    timestamp=ledger_timestamp(),
                    before_hash=before_hash,
                    after_hash=after_hash,
                )
            )
        except Exception:
            logger.exception(
                "Run ledger append failed",
                extra={"path": path, "operation": operation},
            )

    # -- introspection -----------------------------------------------------

    def checkpoint_for(
        self,
        *,
        run_id: str,
        workspace_root: str,
    ) -> Optional[RunCheckpointRef]:
        root = str(Path(workspace_root).expanduser().resolve(strict=False))
        return self._checkpoints.get(run_id, {}).get(root)

    def tracked_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._checkpoints))

    # -- internals ---------------------------------------------------------

    def _require_ref(
        self,
        run_id: str,
        workspace_root: str,
    ) -> RunCheckpointRef:
        ref = self.checkpoint_for(run_id=run_id, workspace_root=workspace_root)
        if ref is None:
            raise AgentPlatformError(
                "checkpoint_not_found",
                f"Run {run_id} 对该工作区没有 checkpoint，无法 restore。",
                retryable=False,
            )
        return ref

    def _load_manifest(self, ref: RunCheckpointRef) -> CheckpointManifest:
        return read_manifest(self._store_root, ref.checkpoint_id)


__all__ = [
    "RestoreRunOutcome",
    "RunCheckpointCoordinator",
    "RunCheckpointRef",
]
