from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.tooling import ToolApprovalMode, ToolRegistry, ToolValidationError

from ..storage.sqlite_mcp_server_repository import (
    McpServerConfig,
    SqliteMcpServerRepository,
)
from ..workspace_runtime.effect_log import EffectLog
from .models import McpServerRuntimeStatus, McpToolStatus
from .tool import McpToolBridge

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 15.0
_CREDENTIAL_KEY = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN)", re.IGNORECASE)
_AWS_PREFIX = re.compile(r"^AWS_", re.IGNORECASE)


def tool_allowed(server: McpServerConfig, raw_name: str) -> bool:
    """F2：单个工具是否在服务器的白名单内（三态语义见仓储的 ``parse_allowed_tools``）。"""
    allowed = server.allowed_tools
    if allowed is None:
        return True
    return raw_name in allowed



@dataclass
class _LiveConnection:
    config: McpServerConfig
    session: Any
    stop_event: asyncio.Event
    failure_event: asyncio.Event
    task: Optional[asyncio.Task[None]]
    tools: tuple[McpToolBridge, ...]
    state: str = "reconnecting"
    connected_at: float = 0.0
    #: M1：工具列表热同步串行化 + 合并（同一时刻只跑一次，期间再有通知合并成一轮）。
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sync_pending: bool = False
    sync_generation: int = 0
    sync_task: Optional[asyncio.Task] = None


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
        workspace_root_provider=None,
        reconnect_initial_delay_seconds: float = 0.5,
        reconnect_max_delay_seconds: float = 30.0,
        reconnect_max_attempts: int = 10,
        health_check_seconds: float = 5.0,
    ) -> None:
        self._repository = repository
        self._registry = tool_registry
        self._connect_timeout_seconds = connect_timeout_seconds
        self._effect_log = effect_log
        self._workspace_root_provider = workspace_root_provider
        self._reconnect_initial_delay_seconds = reconnect_initial_delay_seconds
        self._reconnect_max_delay_seconds = reconnect_max_delay_seconds
        self._reconnect_max_attempts = reconnect_max_attempts
        self._health_check_seconds = health_check_seconds
        self._connections: dict[str, _LiveConnection] = {}
        self._errors: dict[str, str] = {}
        #: F2：``allow_unattended=0`` 的服务器当前暴露的 MCP 注册名全集。未值守过滤器
        #: （wiring 三处）读它，所以两种 execution backend 配置下都生效——默认配置下
        #: 未值守执行器拿的是**裸 registry**，没有这一层过滤就"任务运行仍调得到"。
        self._unattended_excluded: frozenset[str] = frozenset()
        #: F2：管理器是否**见过 MCP 连接**（用来把"没装 MCP 的进程"与"服务器不允许
        #: 未值守"两种情况区分开——后者必须 fail-closed）。
        self._saw_unattended_scope: bool = False
        #: F2：管理器**曾经枚举到**的 MCP 注册名（诊断/测试用）。
        self._enumerated_tool_names: frozenset[str] = frozenset()
        self._task_supervisor = BackgroundTaskSupervisor(
            name="mcp-manager",
            logger=logger,
        )
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
        await self._task_supervisor.drain()
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
                task=None,
                tools=(),
            )
            task = self._task_supervisor.spawn(
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
        task = connection.task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # 主动断开时，对端进程可能已退出，MCP stdio 客户端会在清理阶段抛出
            # ConnectionResetError 等；断开本身是意图内的，不应在 stop/disconnect
            # 中失败，故吞掉连接任务的异常（诊断信息仍保留在 connection.state/errors）。
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
                    await self.handle_notification(message, server, session)

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
                    workspace_root_provider=self._workspace_root_provider,
                )
            )
        return tuple(bridges)

    def _session(self, server_name: str):
        for connection in self._connections.values():
            if connection.config.name == server_name:
                return connection.session
        return None

    async def handle_notification(
        self, message: Any, server: McpServerConfig, session: Any
    ) -> None:
        """M1：只处理 ``notifications/tools/list_changed``，其余通知忽略。"""
        from mcp import types as mcp_types

        root = getattr(message, "root", None)
        if not isinstance(root, mcp_types.ToolListChangedNotification):
            return
        connection = self._connections.get(server.id)
        if connection is None or connection.session is not session:
            return
        # 关键：**不能**在消息处理回调里 await 同步——SDK 的消息循环是串行的，
        # 同步过程还要发 list_tools 请求并等响应，而响应同样要经过这个循环，
        # 直接 await 会死锁。改为投递到后台任务，回调立即返回。
        if connection.sync_task is not None and not connection.sync_task.done():
            connection.sync_pending = True
            return
        connection.sync_task = self._task_supervisor.spawn(
            self.sync_tools(server, connection, session=session),
            name=f"mcp-sync:{server.name}",
        )

    async def sync_tools(
        self,
        server: McpServerConfig,
        connection: "_LiveConnection",
        *,
        session: Any = None,
    ) -> bool:
        """M1：``tools/list_changed`` 后的两阶段代际切换。

        阶段一：拉全量并构建候选工具集（重复 rawName 直接报错，不动旧代）；
        阶段二：用候选集做一次**整体注册校验**，通过才提交（registry + connection
        同时切换）；校验失败或拉取失败都保留旧一代继续服务。

        同一连接上的同步串行化：正在同步时再来的通知只置一个 pending 位，
        由当前这轮循环消费，避免并发注册交错。
        """
        target = session if session is not None else connection.session
        if target is None:
            return False
        if connection.sync_lock.locked():
            connection.sync_pending = True
            return False
        async with connection.sync_lock:
            changed = False
            while True:
                connection.sync_pending = False
                try:
                    candidate = await self._discover_tools(server, target)
                except Exception as error:
                    # 拉取失败：保留旧代，仅记录诊断。
                    self._errors[server.id] = (
                        f"MCP 服务器 {server.name} 工具列表刷新失败：{error}"
                    )
                    return changed
                try:
                    self._registry_replace_with({server.id: candidate})
                except ToolValidationError as error:
                    self._errors[server.id] = (
                        f"MCP 服务器 {server.name} 工具注册冲突，已保留旧一代："
                        f"{error}"
                    )
                    return changed
                connection.tools = candidate
                connection.sync_generation += 1
                connection.state = "connected"
                self._errors.pop(server.id, None)
                changed = True
                if not connection.sync_pending:
                    return changed

    def _registry_replace_with(
        self, overrides: dict[str, tuple[McpToolBridge, ...]]
    ) -> None:
        """用候选工具集做整体替换校验（失败时不修改任何连接状态）。

        F2（ADR 的 Critical-2）：白名单在这里**注册层**过滤——``EnforcingToolRegistry``
        只覆写 ``resolve``，只过滤 ``definitions()`` 是假安全。这里是
        ``tools/list_changed`` 两阶段代际切换（``sync_tools``）的提交点。
        """
        tools = tuple(
            tool
            for connection in self._connections.values()
            for tool in self._authorized(connection.config, overrides.get(connection.config.id, connection.tools))
        )
        self._registry.replace_tools(tools)
        self._recompute_unattended_excluded(self._registry_tool_names())

    def _replace_registry_tools(self) -> None:
        """用当前 ``connection.tools`` 整体重注册（连接/断开/重连路径）。

        F2：与 ``_registry_replace_with`` 是**两个都必须过滤**的注册点——只改一处会被
        另一处静默撤回。
        """
        tools = tuple(
            tool
            for connection in self._connections.values()
            for tool in self._authorized(connection.config, connection.tools)
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
        self._recompute_unattended_excluded(self._registry_tool_names())

    @staticmethod
    def _authorized(
        server: McpServerConfig, tools: tuple[McpToolBridge, ...]
    ) -> tuple[McpToolBridge, ...]:
        """按服务器白名单筛出**允许注册**的工具（按 ``rawName`` 匹配，ADR §2.1）。"""
        return tuple(tool for tool in tools if tool_allowed(server, tool.raw_name))

    def _registry_tool_names(self) -> tuple[str, ...]:
        """当前**已注册进注册表**的 MCP 工具名。

        用注册表而不是连接列表：注册可能失败（``replace_tools`` 抛错时连接状态不变），
        执行面拿到的就是注册表里那批。白名单已在写入时排除，所以这里枚举到的名字
        **就是**允许注册的那批。
        """
        return tuple(
            definition.name for definition in self._registry.definitions()
        )

    def _recompute_unattended_excluded(self, tool_names: tuple[str, ...]) -> None:
        """重算"未值守不可用"的 MCP 注册名集合（F2 / ADR 的 I1）。

        排除集覆盖**这个管理器枚举到的全部 MCP 工具**（不只是注册成功的那些）：
        注册被断开/冲突清掉时，``allow_unattended=0`` 的工具绝不能因为"不在集合里"
        而被放行（安全默认优先于计数准确）。
        """
        self._enumerated_tool_names = frozenset(tool_names)
        if self._connections:
            # "这个管理器见过 MCP 世界"——一旦有过连接就置位，之后即使连接断开、
            # 工具全部反注册，`allow_unattended=0` 的判定也仍然生效（不因为空连接
            # 而回落到"兼容模式"）。
            self._saw_unattended_scope = True
        # 连接里已枚举的工具（含被白名单挡掉、或这一代没注册成功的）：只要服务器不
        # 允许未值守，就按未值守不可用记账——fail-safe 优先于计数准确。
        excluded: set[str] = set()
        for connection in self._connections.values():
            if connection.config.allow_unattended:
                continue
            for tool in connection.tools:
                excluded.add(tool.public_name)
        self._unattended_excluded = frozenset(excluded)

    def allows_unattended(self, tool_name: str) -> bool:
        """F2：该工具在**未值守**运行里是否可用（定时任务 + 委派子运行，ADR 的 I1）。

        两道：
          * 管理器**从未有过 MCP 连接**（没装 MCP 的进程 / 只测注册表面的用例）⇒ ``True``，
            与 F2 之前逐字节一致，避免空管理器误伤；
          * 有过连接 ⇒ 只有"服务器允许未值守且该工具在白名单内"才可用；被禁的、以及
            连接已断开/这一代没注册成功的 MCP 名一律 ``False``。
        """
        if not self._saw_unattended_scope:
            return True
        if tool_name in self._unattended_excluded:
            return False
        return True

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
                requires_explicit_confirmation=(
                    tool.definition.approval_mode is ToolApprovalMode.REQUIRED
                    or tool._destructive
                ),
                # F2（契约新增）：被白名单挡掉的工具**仍然出现在这里**——否则用户收窄
                # 白名单后就再也看不到被排除的工具、无法重新放行。注册表里没有它，
                # 但它作为一份"发现到的工具清单"必须完整。
                allowed=tool_allowed(server, tool.raw_name),
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
