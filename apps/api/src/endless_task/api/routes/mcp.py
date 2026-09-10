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
    def _mcp_json(config, status) -> dict[str, object]:
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
            "state": status.state,
            "toolCount": status.tool_count,
            "lastError": status.last_error,
            "tools": [
                {
                    "publicName": tool.public_name,
                    "rawName": tool.raw_name,
                    "description": tool.description,
                    "effect": tool.effect,
                    "requiresExplicitConfirmation": (
                        tool.requires_explicit_confirmation
                    ),
                }
                for tool in status.tools
            ],
            "createdAt": config.created_at,
            "updatedAt": config.updated_at,
        }


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
            )
        )
        try:
            await container.mcp_manager.reload(config.id)
        except Exception:
            pass
        return {"server": _mcp_json(config, container.mcp_manager.status(config.id))}


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
            )
        )
        try:
            await container.mcp_manager.reload(config.id)
        except Exception:
            pass
        return {"server": _mcp_json(config, container.mcp_manager.status(config.id))}


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
        try:
            status = await container.mcp_manager.reload(server_id)
        except Exception:
            status = container.mcp_manager.status(server_id)
        config = container.mcp_server_repository.get_server(server_id)
        return {"server": _mcp_json(config, status)}
