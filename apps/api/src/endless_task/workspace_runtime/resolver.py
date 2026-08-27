"""会话 → 工作区根解析：fs/shell 工具仅在工作区会话且已绑定 root_path 时可用。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from endless_task.domain.repositories import NotFoundError
from endless_task.storage.sqlite_chat_repository import SqliteChatRepository
from endless_task.storage.sqlite_workspace_repository import SqliteWorkspaceRepository
from endless_task.tooling import ToolError


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    root: Path


class WorkspaceResolver:
    def __init__(
        self,
        chat_repository: SqliteChatRepository,
        workspace_repository: SqliteWorkspaceRepository,
    ) -> None:
        self._chat_repository = chat_repository
        self._workspace_repository = workspace_repository

    def resolve_binding(self, conversation_id: str) -> Optional[WorkspaceBinding]:
        try:
            conversation = self._chat_repository.get_conversation(conversation_id)
        except NotFoundError:
            return None
        workspace_id = conversation.workspace_id
        if not workspace_id:
            return None
        try:
            workspace = self._workspace_repository.get_workspace(workspace_id)
        except NotFoundError:
            return None
        if not workspace.root_path:
            return None
        root = Path(workspace.root_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            return None
        return WorkspaceBinding(workspace_id=workspace_id, root=root)

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        binding = self.resolve_binding(conversation_id)
        if binding is None:
            raise ToolError(
                "workspace_not_bound",
                "当前会话所在的工作区未绑定本地目录，文件系统与 shell 工具不可用。"
                "请先在「工作区设置」里选择并绑定一个本地目录。",
                retryable=False,
            )
        return binding
