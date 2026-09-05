"""M4B isolated-write child scratch workspace (slice P1).

DR-4（06 §30）：write-enabled child 只在**隔离 scratch** 写，绝不直接污染主 workspace。
本模块提供 scratch 的创建/binding：

- scratch = 主 workspace 根下 ``.endless-task/delegation/<child_run_id>/``
  （同一磁盘/备份/containment 语义；该前缀已加入 checkpoint skip，主 run 快照不膨胀）；
- 以独立 ``workspaces`` 行 + child conversation ``workspace_id`` 完成 binding ——
  WorkspaceResolver 因此把 child 的 fs/shell 工具解析到 scratch 根，containment 保证
  child 越不出 scratch（写主 workspace 需路径逃逸 → resolve 拒绝）；
- child 写仅落 scratch → 主 workspace 零变化；child run 的 checkpoint/自动恢复以
  scratch 为根，天然兜底。

P1 阶段只提供能力，不改变任何默认行为（spawn 写模式由 P2 打开）。
"""

from __future__ import annotations

from pathlib import Path


def prepare_isolated_child_workspace(
    *,
    workspace_repository,
    parent_workspace_root: Path,
    child_run_id: str,
) -> str:
    """Create+bind a scratch workspace for one write-enabled child run.

    Returns the new workspace id. Idempotent-ish per child run id (a fresh
    child run gets a fresh scratch dir). ``workspace_repository`` is the
    SqliteWorkspaceRepository (create_workspace + bind_root_path).
    """
    root = Path(parent_workspace_root).expanduser().resolve()
    scratch = root / ".endless-task" / "delegation" / child_run_id
    scratch.mkdir(parents=True, exist_ok=True)
    workspace = workspace_repository.create_workspace(
        name=f"delegation:{child_run_id}"
    )
    try:
        workspace_repository.bind_root_path(workspace.id, str(scratch))
    except Exception:
        # Do not leave an unbound row behind on failure; re-raise.
        try:
            workspace_repository.unbind_root_path(workspace.id)
        except Exception:
            pass
        raise
    return workspace.id


__all__ = ["prepare_isolated_child_workspace"]
