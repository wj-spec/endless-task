"""工作区工具可见性（v1/v2 共用）。

工作区运行时工具（fs/shell）仅在「会话所属工作区已绑定本地目录」时对模型
可见；无绑定（通用）会话必须看不到这些工具，避免模型调用后撞上
workspace_not_bound，甚至触发内部错误。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .resolver import WorkspaceResolver

# 工作区运行时专属工具集。v1（runtime/assistant.py 历史内联）与 v2 共用本集合，
# 修订时必须保持一致。
WORKSPACE_TOOLS: frozenset[str] = frozenset(
    {
        "read_workspace_file",
        "write_workspace_file",
        "list_workspace_dir",
        "delete_workspace_file",
        "run_shell",
        "workspace_search",
    }
)


def workspace_tool_filter(
    resolver: Optional[WorkspaceResolver],
    conversation_id: str,
) -> Optional[Callable[[str], bool]]:
    """构造与 v1 assistant 一致的按会话工具过滤。

    返回 `(name: str) -> bool` 谓词，True = 可见。工作区工具在无绑定时被过滤
    掉；resolver 为 None 时返回 None（不过滤）。会话绑定正常时返回 None。
    """
    if resolver is None:
        return None
    # 会话「未绑定」= 无 workspace_id，或 workspace 无 root_path，或根目录不可用。
    binding = resolver.resolve_binding(conversation_id)
    if binding is None:
        def filter_out(name: str) -> bool:
            return name not in WORKSPACE_TOOLS

        return filter_out
    return None
