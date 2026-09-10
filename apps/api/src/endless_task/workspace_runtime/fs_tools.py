"""文件类工具（聚合模块）。

原先是一个 1 809 行、7 个工具类的文件；现在实现拆到 `workspace_runtime/fs/`：
`common.py`（共享底座：锁表、哈希/解码、行匹配与差异、变更与撤销记录、effect 日志）
+ 一个工具一个文件。这里只做 re-export，`from endless_task.workspace_runtime.fs_tools
import WriteWorkspaceFileTool` 这类既有导入路径保持不变。
"""

from __future__ import annotations

from .effect_log import sha256_text  # noqa: F401  (re-export：拆分前本模块即为导入点)
from .fs.common import (  # noqa: F401  (re-export：既有调用方/测试可能引用)
    _PATH_LOCKS,
    logger,
    _PathLockManager,
    _backend_policy_and_trace,
    _decode_utf8,
    _ensure_checkpoint,
    _line_diff,
    _log_effect,
    _match_line_numbers,
    _now_iso,
    _read_text_or_none,
    _record_mutation,
    _record_undo_delete,
    _record_undo_write,
    _sha256_bytes,
    _undo_context,
)
from .fs.delete_workspace_file import DeleteWorkspaceFileTool
from .fs.edit_workspace_file import EditWorkspaceFileTool
from .fs.list_workspace_dir import ListWorkspaceDirTool, MAX_LIST_ITEMS
from .fs.manage_workspace_paths import ManageWorkspacePathsTool
from .fs.read_skill_file import ReadSkillFileTool
from .fs.read_workspace_file import ReadWorkspaceFileTool
from .fs.write_workspace_file import WriteWorkspaceFileTool

#: 拆分前与本模块同级的常量；仓库内暂无引用，保留以免下游/外部脚本导入报错。
MAX_READ_LINES = 500

__all__ = [
    "DeleteWorkspaceFileTool",
    "EditWorkspaceFileTool",
    "ListWorkspaceDirTool",
    "ManageWorkspacePathsTool",
    "ReadSkillFileTool",
    "ReadWorkspaceFileTool",
    "WriteWorkspaceFileTool",
]
