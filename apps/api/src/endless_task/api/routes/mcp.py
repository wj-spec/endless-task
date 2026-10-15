"""mcp 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI, Query

from endless_task.storage.sqlite_mcp_server_repository import McpServerDraft

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.mcp import McpServerBody, McpServerPatchBody


def _mcp_call_status(detail: str) -> str:
    for part in detail.split(";"):
        text = part.strip()
        if text.startswith("status="):
            return text[len("status=") :]
    return "ok"


def register_mcp_routes(app: FastAPI, container: AppContainer) -> None:
    def _mcp_json(config, status, *, reload_error: str | None = None) -> dict[str, object]:
        return {
            "id": config.id,
            "name": config.name,
            "transport": config.transport,
            "command": config.command,
            "args": list(config.args),
            "cwd": config.cwd,
            "url": config.url,
            "enabled": config.enabled,
            "toolCallTimeoutSeconds": config.tool_call_timeout_seconds,
            # F2（契约新增）：白名单三态（None = 未配置 = 全部允许；[] = 全部禁用）。
            "allowedTools": (
                None if config.allowed_tools is None else list(config.allowed_tools)
            ),
            # F2（契约新增）：未值守边界（新服务器默认 false）。
            "allowUnattended": config.allow_unattended,
            "state": status.state,
            "toolCount": status.tool_count,
            "lastError": status.last_error,
            # F2（§6 处置 (a)）：重载失败时**显式**回报"已保存但未重新加载工具"。
            # 改白名单后 reload 若静默失败，同一次响应里 ``allowedTools``（新配置）与
            # ``tools``（旧连接）会自相矛盾，而用户以为已生效。这里把失败做成可见字段。
            "reloadError": reload_error,
            "tools": [
                {
                    "publicName": tool.public_name,
                    "rawName": tool.raw_name,
                    "description": tool.description,
                    "effect": tool.effect,
                    "requiresExplicitConfirmation": (
                        tool.requires_explicit_confirmation
                    ),
                    # F2（契约新增）：被白名单挡掉的工具仍出现在列表里，只是这里为 false。
                    "allowed": tool.allowed,
                }
                for tool in status.tools
            ],
            "createdAt": config.created_at,
            "updatedAt": config.updated_at,
        }

    async def _reload_or_report(server_id: str) -> str | None:
        """重载服务器；失败时返回可呈现的错误文本（不抛、不静默）。

        F2 §6：三个调用点原来都 ``except Exception: pass``。改白名单让"保存后必须立刻
        生效"成为用户预期，所以失败必须可见。返回值走响应里的 ``reloadError``。
        """
        try:
            await container.mcp_manager.reload(server_id)
        except Exception as error:  # noqa: BLE001 连接失败是预期形态，交给 UI 呈现
            return str(error) or error.__class__.__name__
        return None


    @app.get("/mcp/servers")
    async def list_mcp_servers() -> dict[str, object]:
        items = []
        for config in container.mcp_server_repository.list_servers():
            items.append(_mcp_json(config, container.mcp_manager.status(config.id)))
        return {"items": items}


    @app.post("/mcp/servers", status_code=201)
    async def create_mcp_server(body: McpServerBody) -> dict[str, object]:
        config = container.mcp_server_repository.create_server(
            McpServerDraft(
                name=body.name,
                transport=body.transport,
                command=body.command,
                args=tuple(body.args),
                env=body.env,
                cwd=body.cwd,
                url=body.url,
                headers=body.headers,
                enabled=body.enabled,
                tool_call_timeout_seconds=body.toolCallTimeoutSeconds,
                allowed_tools=(
                    None if body.allowedTools is None else tuple(body.allowedTools)
                ),
                allow_unattended=body.allowUnattended,
            )
        )
        reload_error = await _reload_or_report(config.id)
        return {
            "server": _mcp_json(
                config,
                container.mcp_manager.status(config.id),
                reload_error=reload_error,
            )
        }


    @app.patch("/mcp/servers/{server_id}")
    async def patch_mcp_server(
        server_id: str, body: McpServerPatchBody
    ) -> dict[str, object]:
        current = container.mcp_server_repository.get_server(server_id)
        name = body.name if body.name is not None else current.name
        transport = body.transport if body.transport is not None else current.transport
        command = body.command if body.command is not None else current.command
        args = tuple(body.args) if body.args is not None else current.args
        env = body.env if body.env is not None else current.env
        cwd = body.cwd if body.cwd is not None else current.cwd
        url = body.url if body.url is not None else current.url
        headers = body.headers if body.headers is not None else current.headers
        enabled = body.enabled if body.enabled is not None else current.enabled
        timeout = (
            body.toolCallTimeoutSeconds
            if body.toolCallTimeoutSeconds is not None
            else current.tool_call_timeout_seconds
        )
        # F2（ADR 的 Critical-1）：两个新列同样"缺省 = 保留现值"。漏掉这里，
        # F1 的启用/禁用开关每点一次就会把白名单重置为 NULL（= 全允许）、
        # 把 allow_unattended 重置为列默认——静默吞数据。
        #
        # F2 修复波（复审 C1）：但 ``allowedTools`` 必须用 ``model_fields_set`` 区分
        # "**缺字段**"与"**显式 null**"，两者语义不同（见 ``schemas/mcp.py`` 的 I6 契约）：
        #   * 缺字段      ⇒ 保留现值（上面那条防吞数据的口径）；
        #   * 显式 ``null`` ⇒ **重置为未配置 = 全部允许**。前端"全选"保存时发的就是
        #     ``{"allowedTools": null}``（``McpManagement.tsx`` 的 ``allowedToolsPatch``），
        #     旧写法把它并进"缺字段"分支 ⇒ 请求 200、提示"已保存"，而库里白名单纹丝不动，
        #     且 ``allowed_tools_json`` 一旦非 NULL 就再也回不到 NULL（I6 语义不可达）。
        # 跨端回归：``tests/test_mcp_tool_allowlist.py`` 的
        # ``test_explicit_null_allowlist_resets_to_all_allowance``。
        if "allowedTools" in body.model_fields_set:
            allowed_tools = (
                None if body.allowedTools is None else tuple(body.allowedTools)
            )
        else:
            allowed_tools = current.allowed_tools
        # 口径差异是**刻意**的（不是漏改）：``allowUnattended`` 是布尔开关，"取消勾选"
        # 用 ``false`` 表达，前端从不发 ``null``；而 ``true``/``false`` 都是显式值，
        # 旧写法只能把 ``null`` 当保留——没有"显式 null = 重置为列默认"的可用语义
        # （重置为默认反而会把开关关掉，与用户意图相反）。所以这里维持 ``is not None``，
        # 并在 F2 修复报告里登记为已知口径。
        allow_unattended = (
            body.allowUnattended
            if body.allowUnattended is not None
            else current.allow_unattended
        )
        config = container.mcp_server_repository.update_server(
            server_id,
            McpServerDraft(
                name=name,
                transport=transport,
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                url=url,
                headers=headers,
                enabled=enabled,
                tool_call_timeout_seconds=timeout,
                allowed_tools=allowed_tools,
                allow_unattended=allow_unattended,
            )
        )
        reload_error = await _reload_or_report(config.id)
        return {
            "server": _mcp_json(
                config,
                container.mcp_manager.status(config.id),
                reload_error=reload_error,
            )
        }


    @app.delete("/mcp/servers/{server_id}", status_code=204)
    async def delete_mcp_server(server_id: str) -> None:
        await container.mcp_manager.disconnect(server_id)
        container.mcp_server_repository.delete_server(server_id)


    @app.get("/mcp/servers/{server_id}/calls")
    async def list_mcp_calls(
        server_id: str,
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, object]:
        """M2：某 MCP 服务器的最近调用（耗时/状态/结果规模）。"""
        config = container.mcp_server_repository.get_server(server_id)
        entries = container.effect_log.list_for_operation(
            f"mcp__{config.name}__", limit=limit
        )
        return {
            "items": [
                {
                    "time": entry.get("time"),
                    "operation": entry.get("operation"),
                    "detail": entry.get("detail"),
                    "durationMs": entry.get("durationMs", 0),
                    "status": _mcp_call_status(str(entry.get("detail", ""))),
                }
                for entry in entries
            ]
        }


    @app.post("/mcp/servers/{server_id}/reload")
    async def reload_mcp_server(server_id: str) -> dict[str, object]:
        # F2 §6 处置 (a)：显式 reload 也把失败如实回报（原来连"重连"按钮都没法报错）。
        reload_error = await _reload_or_report(server_id)
        config = container.mcp_server_repository.get_server(server_id)
        return {
            "server": _mcp_json(
                config,
                container.mcp_manager.status(server_id),
                reload_error=reload_error,
            )
        }
