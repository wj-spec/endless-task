"""工作区绑定 API 测试辅助。

必选绑定产品设定（docs/workspace-mandatory-binding-design.md）要求：新建会话必须
归属到一个已绑定本地目录的工作区。历史测试直接 `POST /conversations`（无
workspaceId），现在会得到 409。本模块提供两个 helper：

- ``create_bound_workspace``：创建并绑定一个临时目录作为工作区根。
- ``create_bound_conversation``：先建绑定工作区，再在其下建会话，返回会话 JSON。
"""

from __future__ import annotations

import tempfile
import uuid

import httpx


async def create_bound_workspace(
    client: httpx.AsyncClient,
    *,
    name: str | None = None,
) -> str:
    """创建并绑定一个临时目录，返回工作区 id。

    每次调用使用随机名，避免同名工作区触发唯一约束冲突。
    """
    workspace_name = name or f"测试工作区 {uuid.uuid4().hex[:8]}"
    workspace = await client.post(
        "/workspaces",
        json={
            "name": workspace_name,
            "rootPath": tempfile.mkdtemp(prefix="ws-"),
        },
    )
    workspace.raise_for_status()
    return workspace.json()["workspace"]["id"]


async def create_bound_conversation(
    client: httpx.AsyncClient,
) -> dict[str, object]:
    """建绑定工作区并创建会话，返回会话 JSON。"""
    workspace_id = await create_bound_workspace(client)
    response = await client.post("/conversations", json={"workspaceId": workspace_id})
    response.raise_for_status()
    return response.json()
