from __future__ import annotations

import asyncio
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from endless_task.tooling import ToolRegistry, ToolValidationError

from ..storage.sqlite_mcp_server_repository import (
    McpServerConfig,
    SqliteMcpServerRepository,
)
from ..workspace_runtime.effect_log import EffectLog
from .models import McpServerRuntimeStatus, McpToolStatus
from .tool import McpToolBridge

CONNECT_TIMEOUT_SECONDS = 15.0
_CREDENTIAL_KEY = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN)", re.IGNORECASE)
_AWS_PREFIX = re.compile(r"^AWS_", re.IGNORECASE)


@dataclass
class _LiveConnection:
    config: McpServerConfig
    session: Any
    stop_event: asyncio.Event
    failure_event: asyncio.Event
    task: asyncio.Task
    tools: tuple[McpToolBridge, ...]
    state: str = "reconnecting"
    connected_at: float = 0.0


def _sanitized_environment(explicit: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not _CREDENTIAL_KEY.search(key) and not _AWS_PREFIX.match(key)
    }
    environment.update(dict(explicit))
    return environment


class McpManager:
    def __init__(
        self,
        *,
        repository: SqliteMcpServerRepository,
        tool_registry: ToolRegistry,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        effect_log: Optional[EffectLog] = None,
        reconnect_initial_delay_seconds: float = 0.5,
        reconnect_max_delay_seconds: float = 30.0,
        reconnect_max_attempts: int = 10,
        health_check_seconds: float = 5.0,
    ) -> None:
        self._repository = repository
        self._registry = tool_registry
        self._connect_timeout_seconds = connect_timeout_seconds
        self._effect_log = effect_log
        self._reconnect_initial_delay_seconds = reconnect_initial_delay_seconds
        self._reconnect_max_delay_seconds = reconnect_max_delay_seconds
        self._reconnect_max_attempts = reconnect_max_attempts
        self._health_check_seconds = health_check_seconds
        self._connections: dict[str, _LiveConnection] = {}
        self._errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        for server in self._repository.list_servers(enabled_only=True):
            try:
                await self.connect(server.id)
            except Exception as error:
                self._errors[server.id] = str(error)

    async def stop_all(self) -> None:
        for server_id in list(self._connections):
            await self.disconnect(server_id)
        self._registry.remove_tools()

    async def connect(self, server_id: str) -> McpServerRuntimeStatus:
        server = self._repository.get_server(server_id)
        async with self._lock:
            await self._disconnect_locked(server_id)
            self._errors.pop(server_id, None)
            stop_event = asyncio.Event()
            failure_event = asyncio.Event()
            ready: asyncio.Future[tuple[Any, tuple[McpToolBridge, ...]]] = (
                asyncio.get_running_loop().create_future()
            )
            connection = _LiveConnection(
                config=server,
                session=None,
                stop_event=stop_event,
                failure_event=failure_event,
                task=None,  # type: ignore[arg-type]
                tools=(),
            )
            task = asyncio.create_task(
                self._supervise_server(
                    server, stop_event, failure_event, ready, connection
                ),
                name=f"mcp:{server.name}",
            )
            connection.task = task
            self._connections[server.id] = connection
            try:
                await asyncio.wait_for(ready, timeout=self._connect_timeout_seconds)
                if ready.exception() is not None:
                    raise ready.exception()
            except asyncio.TimeoutError as error:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)
                self._connections.pop(server.id, None)
                self._replace_registry_tools()
                raise RuntimeError(
                    f"连接 MCP 服务器 {server.name} 超时。"
                ) from error
            except Exception:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)
                self._connections.pop(server.id, None)
                self._replace_registry_tools()
                raise
        return self.status(server_id)

    async def disconnect(self, server_id: str) -> None:
        async with self._lock:
            await self._disconnect_locked(server_id)
            self._replace_registry_tools()

    async def reload(self, server_id: str) -> McpServerRuntimeStatus:
        server = self._repository.get_server(server_id)
        if not server.enabled:
            await self.disconnect(server_id)
            return self.status(server_id)
        return await self.connect(server_id)

    async def _disconnect_locked(self, server_id: str) -> None:
        connection = self._connections.pop(server_id, None)
        if connection is None:
            return
        connection.stop_event.set()
        try:
            await connection.task
        except asyncio.CancelledError:
            pass

    async def _supervise_server(
        self,
        server: McpServerConfig,
        stop_event: asyncio.Event,
        failure_event: asyncio.Event,
        ready: asyncio.Future[tuple[Any, tuple[McpToolBridge, ...]]],
        connection: _LiveConnection,
    ) -> None:
        attempts = 0
        first_attempt = True
        while not stop_event.is_set():
            failure_event.clear()
            started_at = asyncio.get_running_loop().time()
            run_task = asyncio.create_task(
                self._run_server_once(
                    server, stop_event, failure_event, ready, connection
                )
            )
            stop_task = asyncio.create_task(stop_event.wait())
            failure_task = asyncio.create_task(failure_event.wait())
            await asyncio.wait(
                {run_task, stop_task, failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_event.is_set():
                failure_event.set()
                await self._cancel_generation(run_task, stop_task, failure_task)
                return

            if not run_task.done():
                run_task.cancel()
                try:
                    await asyncio.wait_for(run_task, 1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            await self._cancel_generation(
                None, stop_task, failure_task
            )

            if first_attempt and ready.exception() is not None:
                return
            first_attempt = False

            elapsed = asyncio.get_running_loop().time() - started_at
            if elapsed >= self._reconnect_max_delay_seconds:
                attempts = 0
            attempts += 1
            if attempts >= self._reconnect_max_attempts:
                self._errors[server.id] = (
                    f"MCP 服务器 {server.name} 连续重连失败，已停止自动重连。"
                )
                self._connections.pop(server.id, None)
                try:
                    self._replace_registry_tools()
                except ToolValidationError:
                    pass
                return

            delay = min(
                self._reconnect_initial_delay_seconds * (2 ** (attempts - 1)),
                self._reconnect_max_delay_seconds,
            )
            connection.state = "reconnecting"
            connection.session = None
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    async def _cancel_generation(
        self,
        run_task: Optional[asyncio.Task],
        *waiters: asyncio.Task,
    ) -> None:
        tasks = (
            (run_task,) if run_task is not None else ()
        ) + tuple(waiters)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_server_once(
        self,
        server: McpServerConfig,
        stop_event: asyncio.Event,
        failure_event: asyncio.Event,
        ready: asyncio.Future[tuple[Any, tuple[McpToolBridge, ...]]],
        connection: _LiveConnection,
    ) -> None:
        async with AsyncExitStack() as stack:
            try:
                if server.transport == "stdio":
                    from mcp import ClientSession, StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    parameters = StdioServerParameters(
                        command=server.command,
                        args=list(server.args),
                        env=_sanitized_environment(server.env),
                        cwd=server.cwd or None,
                    )
                    read_stream, write_stream = await stack.enter_async_context(
                        stdio_client(parameters)
                    )
                else:
                    from mcp import ClientSession
                    from mcp.client.streamable_http import streamablehttp_client

                    streams = await stack.enter_async_context(
                        streamablehttp_client(
                            server.url,
                            headers=dict(server.headers),
                            timeout=30.0,
                        )
                    )
                    read_stream, write_stream, _ = streams
                from mcp import types as mcp_types

                async def handle_message(message: Any) -> None:
                    root = getattr(message, "root", None)
                    if not isinstance(root, mcp_types.ToolListChangedNotification):
                        return
                    connection = self._connections.get(server.id)
                    if connection is None or connection.session is not session:
                        return
                    try:
                        tools = await self._discover_tools(server, session)
                        connection.tools = tools
                        self._replace_registry_tools()
                    except Exception as error:
                        self._errors[server.id] = str(error)

                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        message_handler=handle_message,
                    )
                )
                await asyncio.wait_for(session.initialize(), 10.0)
                tools = await self._discover_tools(server, session)
                connection.session = session
                connection.tools = tools
                connection.state = "connected"
                connection.connected_at = asyncio.get_running_loop().time()
                self._errors.pop(server.id, None)
                if not ready.done():
                    ready.set_result((session, tools))
                self._replace_registry_tools()
                await self._hold_connection(session, stop_event, failure_event)
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except Exception as error:
                self._errors[server.id] = str(error)
                if not ready.done():
                    ready.set_exception(error)

    async def _hold_connection(
        self,
        session: Any,
        stop_event: asyncio.Event,
        failure_event: asyncio.Event,
    ) -> None:
        stop_task = asyncio.create_task(stop_event.wait())
        failure_task = asyncio.create_task(failure_event.wait())
        try:
            while not stop_event.is_set() and not failure_event.is_set():
                done, _ = await asyncio.wait(
                    {stop_task, failure_task},
                    timeout=self._health_check_seconds,
                )
                if done:
                    return
                try:
                    await asyncio.wait_for(session.send_ping(), 1.0)
                except Exception:
                    failure_event.set()
                    return
        finally:
            for task in (stop_task, failure_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, failure_task, return_exceptions=True)

    def _report_tool_failure(self, server_name: str) -> None:
        for connection in self._connections.values():
            if connection.config.name != server_name:
                continue
            connection.state = "reconnecting"
            connection.session = None
            connection.failure_event.set()

    async def _discover_tools(
        self, server: McpServerConfig, session: Any
    ) -> tuple[McpToolBridge, ...]:
        raw_tools: list[Any] = []
        cursor: Optional[str] = None
        while True:
            result = await session.list_tools(cursor)
            raw_tools.extend(result.tools)
            cursor = result.nextCursor
            if not cursor:
                break
        raw_names = [str(tool.name) for tool in raw_tools]
        if len(raw_names) != len(set(raw_names)):
            raise RuntimeError(f"MCP 服务器 {server.name} 返回了重复的工具名。")
        bridges: list[McpToolBridge] = []
        for raw_tool in raw_tools:
            annotations = getattr(raw_tool, "annotations", None)
            read_only = bool(getattr(annotations, "readOnlyHint", False))
            destructive = bool(getattr(annotations, "destructiveHint", False))
            description = str(getattr(raw_tool, "description", "") or "").strip()
            if not description:
                description = f"MCP tool {raw_tool.name} from {server.name}."
            description = description[:1024]
            schema = getattr(raw_tool, "inputSchema", None)
            if not isinstance(schema, Mapping) or schema.get("type") != "object":
                schema = {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }
            bridges.append(
                McpToolBridge(
                    server_name=server.name,
                    raw_name=str(raw_tool.name),
                    description=description,
                    input_schema=schema,
                    read_only=read_only,
                    destructive=destructive,
                    timeout_seconds=server.tool_call_timeout_seconds,
                    session_provider=self._session,
                    effect_log=self._effect_log,
                    failure_callback=self._report_tool_failure,
                )
            )
        return tuple(bridges)

    def _session(self, server_name: str):
        for connection in self._connections.values():
            if connection.config.name == server_name:
                return connection.session
        return None

    def _replace_registry_tools(self) -> None:
        tools = tuple(
            tool
            for connection in self._connections.values()
            for tool in connection.tools
        )
        try:
            self._registry.replace_tools(tools)
        except ToolValidationError:
            self._errors.update(
                {
                    connection.config.id: "MCP 工具注册失败：名称冲突或定义无效。"
                    for connection in self._connections.values()
                }
            )
            raise

    def status(self, server_id: str) -> McpServerRuntimeStatus:
        server = self._repository.get_server(server_id)
        connection = self._connections.get(server_id)
        if connection is not None:
            state = connection.state
            last_error = self._errors.get(server_id)
        elif not server.enabled:
            state = "disabled"
            last_error = self._errors.get(server_id)
        elif server_id in self._errors:
            state = "error"
            last_error = self._errors[server_id]
        else:
            state = "disconnected"
            last_error = None
        tools = tuple(
            McpToolStatus(
                public_name=tool.public_name,
                raw_name=tool.raw_name,
                description=tool.definition.description,
                effect=tool.definition.effect.value,
                requires_explicit_confirmation=tool._destructive,
            )
            for tool in (connection.tools if connection else ())
        )
        return McpServerRuntimeStatus(
            server_id=server.id,
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            state=state,
            tool_count=len(tools),
            last_error=last_error,
            tools=tools,
        )

    def list_statuses(self) -> tuple[McpServerRuntimeStatus, ...]:
        return tuple(
            self.status(server.id)
            for server in self._repository.list_servers()
        )
