"""批次 F Task 2（F2）：MCP 每服务器工具白名单 + 未值守边界。

覆盖四层（ADR 的 Critical-1 / Critical-2 / I1 / I2）：

* **仓储**：两列的往返与三态解析（``NULL`` / 空串 / ``[]`` / 非空数组 / 坏 JSON）；
* **PATCH "缺省 = 保留"**（Critical-1）：先写白名单 + 未值守，再单独 PATCH ``enabled``，
  两列都必须还在——这是"每点一次开关就把白名单重置"的那条回归线；
* **注册层两处过滤**（Critical-2）：``_replace_registry_tools``（连接/断开/重连）与
  ``_registry_replace_with``（``tools/list_changed`` 代际切换）**各自**都要挡住被禁工具，
  且**执行面**（``registry.resolve``）也拿不到；
* **未值守三处作用域**（I1）：任务运行过滤器、委派子运行过滤器、以及执行面
  （协调器 ``_prepare_tool_batch`` 的同一谓词闸），在
  ``ENDLESS_TASK_EXECUTION_BACKEND`` **空 / 非空**两种配置下都要挡住
  ``allow_unattended=0`` 的工具。

F1 复审交接的后端缺口（``PATCH`` 缺 ``env`` = 保留已存密钥）也在本文件补上。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from endless_task.api import AppSettings, create_app
from endless_task import api as _api_module
from endless_task.mcp_runtime.manager import McpManager, _LiveConnection
from endless_task.mcp_runtime.tool import McpToolBridge
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import ProviderToolCall
from endless_task.runtime_v2 import (
    Actor,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.execution_impl.coordinator import _ToolWorkItem
from endless_task.storage import Database
from endless_task.storage.sqlite_mcp_server_repository import (
    McpServerDraft,
    SqliteMcpServerRepository,
    parse_allowed_tools,
)
from endless_task.tooling import ToolApprovalMode, ToolRegistry

from tests.fixtures.workspace_client import create_bound_conversation


def _bridge(server_name: str, raw_name: str, *, read_only: bool = True) -> McpToolBridge:
    return McpToolBridge(
        server_name=server_name,
        raw_name=raw_name,
        description=f"tool {raw_name}",
        input_schema={"type": "object", "properties": {}},
        read_only=read_only,
        destructive=False,
        timeout_seconds=5.0,
        session_provider=lambda _name: None,
    )


def _mcp_names(registry: ToolRegistry) -> list[str]:
    return sorted(
        item.name
        for item in registry.definitions()
        if item.name.startswith("mcp__")
    )


def _raw_mcp_columns(database: Database, server_id: str) -> tuple[Any, Any]:
    """直接读库：``allowed_tools_json`` / ``allow_unattended`` 的**原始列值**。"""
    with database.connect() as connection:
        row = connection.execute(
            "SELECT allowed_tools_json, allow_unattended FROM mcp_servers "
            "WHERE id = ?",
            (server_id,),
        ).fetchone()
    return row["allowed_tools_json"], row["allow_unattended"]


class _RepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "allowlist.db")
        self.database.initialize()
        self.repository = SqliteMcpServerRepository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _raw_columns(self, server_id: str) -> tuple[Any, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT allowed_tools_json, allow_unattended FROM mcp_servers "
                "WHERE id = ?",
                (server_id,),
            ).fetchone()
        return row["allowed_tools_json"], row["allow_unattended"]


class AllowedToolsParsingTest(unittest.TestCase):
    """三态解析必须显式区分 NULL / 空串 / ``[]``（ADR 的 I6：不许 ``json.loads(x or "[]")``）。"""

    def test_null_and_blank_mean_unconfigured(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_allowed_tools(raw))

    def test_empty_array_means_all_disabled(self) -> None:
        self.assertEqual((), parse_allowed_tools("[]"))

    def test_non_empty_array_is_the_allowlist(self) -> None:
        self.assertEqual(
            ("echo", "sum"), parse_allowed_tools('["echo", "sum"]')
        )

    def test_broken_json_falls_back_to_unconfigured(self) -> None:
        # 一行坏 JSON 不该让整个服务器不可用；按"未配置 = 全部允许"处理。
        self.assertIsNone(parse_allowed_tools("{not json"))


class McpAllowlistRepositoryTest(_RepositoryCase):
    def test_new_server_defaults_to_all_allowed_and_unattended_disabled(self) -> None:
        created = self.repository.create_server(
            McpServerDraft(name="demo", transport="stdio", command="node")
        )
        self.assertIsNone(created.allowed_tools)
        self.assertFalse(created.allow_unattended)
        raw_allowed, raw_unattended = self._raw_columns(created.id)
        self.assertIsNone(raw_allowed)
        self.assertEqual(0, raw_unattended)

    def test_explicit_values_round_trip(self) -> None:
        created = self.repository.create_server(
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="node",
                allowed_tools=("echo",),
                allow_unattended=True,
            )
        )
        self.assertEqual(("echo",), created.allowed_tools)
        self.assertTrue(created.allow_unattended)
        re_read = self.repository.get_server(created.id)
        self.assertEqual(("echo",), re_read.allowed_tools)
        self.assertTrue(re_read.allow_unattended)

    def test_empty_tuple_is_stored_as_empty_array_not_null(self) -> None:
        created = self.repository.create_server(
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="node",
                allowed_tools=(),
            )
        )
        self.assertEqual((), created.allowed_tools)
        raw_allowed, _ = self._raw_columns(created.id)
        self.assertEqual("[]", raw_allowed)

    def test_duplicate_and_blank_tool_names_are_rejected(self) -> None:
        for allowed in (("echo", "echo"), ("echo", ""), ("echo", "   ")):
            with self.subTest(allowed=allowed):
                with self.assertRaises(Exception):
                    self.repository.create_server(
                        McpServerDraft(
                            name="demo",
                            transport="stdio",
                            command="node",
                            allowed_tools=allowed,
                        )
                    )

    def test_update_server_writes_both_new_columns(self) -> None:
        """ADR 的 Critical-1 的第 ③ 处：``UPDATE`` 的列清单必须带上两个新列。

        PATCH 走 API 也覆盖这条（下面的 ``McpPatchPreservesNewColumnsTest``），但这里是
        **仓储层直接**的钉子：``update_server`` 少写一列就当场红。
        """
        created = self.repository.create_server(
            McpServerDraft(name="demo", transport="stdio", command="node")
        )
        self.assertIsNone(created.allowed_tools)
        self.assertFalse(created.allow_unattended)

        updated = self.repository.update_server(
            created.id,
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="node",
                allowed_tools=("echo",),
                allow_unattended=True,
            ),
        )
        self.assertEqual(("echo",), updated.allowed_tools)
        self.assertTrue(updated.allow_unattended)
        raw_allowed, raw_unattended = self._raw_columns(created.id)
        self.assertEqual('["echo"]', raw_allowed)
        self.assertEqual(1, raw_unattended)

        # 反向：更新时显式回到"未配置 / 未值守关"，两列都要真的被写回去。
        reverted = self.repository.update_server(
            created.id,
            McpServerDraft(name="demo", transport="stdio", command="node"),
        )
        self.assertIsNone(reverted.allowed_tools)
        self.assertFalse(reverted.allow_unattended)
        raw_allowed, raw_unattended = self._raw_columns(created.id)
        self.assertIsNone(raw_allowed)
        self.assertEqual(0, raw_unattended)


class MigrationBackfillTest(unittest.TestCase):
    """迁移 076 的**回填语义**（ADR §2.1）：既有行回填 1、之后新建的行走列默认 0。

    为什么这条不能只靠脚本证据：整个后端测试里没有别的东西会**红**在
    "076 的 UPDATE 被去掉"上（本用例写之前实测：去掉那条 UPDATE 时全套仍绿）。
    做法是把仓库里 **034 与 075 的真实 SQL**（`SQL_075` 只取其中的
    `CREATE UNIQUE INDEX`）打进一份"停在 075"的库，插入两行**既有**数据，
    再走 `Database.initialize()` 这条生产迁移路径。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "backfill.db"
        # 刻意**不**先 initialize()：本用例要从"停在 075"的状态开始，
        # 所以下面手工记账 001–075（生产迁移路径由 initialize() 在最后走一遍）。
        self.database = Database(self.path)
        self.migrations_dir = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "endless_task"
            / "storage"
            / "migrations"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_075_state(self) -> None:
        """手工把库做成"跑完 075、076 还没跑"：34 的真实建表 + 075 的两条索引 + 记账。"""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.executescript(
                (self.migrations_dir / "034_mcp_servers.sql").read_text(
                    encoding="utf-8"
                )
            )
            # 075 的两条索引刻意**不建**（它们只关乎 `conversations`，与本用例无关），
            # 但在 `schema_migrations` 里照记——`_migrate` 只按记名跳过，
            # 所以 `initialize()` 只会跑 076（这正是要测的那一个）。
            for index in range(1, 76):
                name = next(
                    item.name
                    for item in sorted(self.migrations_dir.glob("*.sql"))
                    if item.name.startswith(f"{index:03d}_")
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (name, applied_at) "
                    "VALUES (?, '2026-01-01T00:00:00.000Z')",
                    (name,),
                )
            connection.commit()

    def _seed_stopped_at_075(self) -> None:
        """把库改造成"076 还没跑、mcp_servers 里有两行既有数据"。"""
        self._fake_075_state()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, args_json, env_json, cwd,
                    url, headers_json, enabled, tool_call_timeout_seconds,
                    created_at, updated_at
                ) VALUES ('mcp_a', 'alpha', 'stdio', 'node', '[]', '{}', '', '',
                          '{}', 1, 60,
                          '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')
                """
            )
            connection.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, args_json, env_json, cwd,
                    url, headers_json, enabled, tool_call_timeout_seconds,
                    created_at, updated_at
                ) VALUES ('mcp_b', 'beta', 'http', '', '[]', '{}', '', '', '{}', 1, 60,
                          '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')
                """
            )
            connection.commit()
        # 前置断言：这时候 mcp_servers 还**没有**新列（否则用例是空转的）。
        with self.database.connect() as connection:
            columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(mcp_servers)")
            ]
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM mcp_servers"
            ).fetchone()["count"]
        self.assertNotIn("allowed_tools_json", columns)
        self.assertNotIn("allow_unattended", columns)
        self.assertEqual(2, count)

    def test_existing_rows_are_backfilled_to_one_and_new_rows_default_to_zero(
        self,
    ) -> None:
        self._seed_stopped_at_075()
        raw_before = self._raw_rows()
        self.assertNotIn("allow_unattended", raw_before[0])
        self.assertEqual(2, len(raw_before))

        self.database.initialize()  # 生产迁移路径：跑 076

        after = self._raw_rows()
        self.assertEqual([1, 1], [row["allow_unattended"] for row in after])
        self.assertEqual([None, None], [row["allowed_tools_json"] for row in after])

        # 迁移**之后**新建的行走列默认 0（fail-safe）。
        from endless_task.storage.sqlite_mcp_server_repository import (
            McpServerDraft,
            SqliteMcpServerRepository,
        )

        created = SqliteMcpServerRepository(self.database).create_server(
            McpServerDraft(name="gamma", transport="stdio", command="node")
        )
        self.assertFalse(created.allow_unattended)
        by_id = {row["id"]: row for row in self._raw_rows()}
        self.assertEqual(0, by_id[created.id]["allow_unattended"])
        self.assertEqual(
            [1, 1, 0], [row["allow_unattended"] for row in self._raw_rows()]
        )
        self.assertEqual("076_mcp_server_tool_allowlist.sql",
                         self.database.applied_migrations()[-1])
        # 幂等：再跑一次不改变任何东西。
        before = self._raw_rows()
        self.database.initialize()
        self.assertEqual(before, self._raw_rows())

    def _raw_rows(self) -> list[dict]:
        with self.database.connect() as connection:
            columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(mcp_servers)")
            ]
            select = ", ".join(columns)
            rows = connection.execute(
                f"SELECT {select} FROM mcp_servers ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]


class McpPatchPreservesNewColumnsTest(unittest.IsolatedAsyncioTestCase):
    """ADR 的 Critical-1：PATCH 重建整份 draft + ``UPDATE`` 写满全部列。

    新列若没接进 ``McpServerDraft`` / ``_validated_values`` / ``UPDATE`` 列清单 /
    PATCH 的 coalesce 四处里的任何一处，下面第二条用例就会红。
    """

    @asynccontextmanager
    async def _client(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client

    async def _app(self, directory: str):
        app = create_app(
            settings=AppSettings(
                database_path=Path(directory) / "patch.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
            ),
            provider=None,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        return app, lifespan

    async def test_patching_enabled_alone_keeps_allowlist_and_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, lifespan = await self._app(directory)
            try:
                container = app.state.container
                async with self._client(app) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "enabled": False,
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    server_id = created.json()["server"]["id"]

                    seeded = await client.patch(
                        f"/mcp/servers/{server_id}",
                        json={
                            "allowedTools": ["echo"],
                            "allowUnattended": True,
                        },
                    )
                    self.assertEqual(200, seeded.status_code)
                    self.assertEqual(["echo"], seeded.json()["server"]["allowedTools"])
                    self.assertTrue(seeded.json()["server"]["allowUnattended"])

                    # ← 一次"只"改 enabled 的 PATCH（F1 的启用/禁用开关就是这一下）。
                    toggled = await client.patch(
                        f"/mcp/servers/{server_id}", json={"enabled": False}
                    )
                    self.assertEqual(200, toggled.status_code)
                    payload = toggled.json()["server"]
                    self.assertEqual(["echo"], payload["allowedTools"])
                    self.assertTrue(payload["allowUnattended"])

                config = container.mcp_server_repository.get_server(server_id)
                self.assertEqual(("echo",), config.allowed_tools)
                self.assertTrue(config.allow_unattended)
                raw_allowed, raw_unattended = _raw_mcp_columns(container.database, server_id)
                self.assertEqual('["echo"]', raw_allowed)
                self.assertEqual(1, raw_unattended)
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_explicit_null_allowlist_resets_to_all_allowance(self) -> None:
        """**修复波 C1 的跨端回归**：显式 ``{"allowedTools": null}`` = "回到全部允许"。

        契约两端（``McpManagement.test.tsx:661``「全选 ⇒ 显式发 null」与
        ``test_patching_enabled_alone_keeps_allowlist_and_unattended``「缺字段 ⇒ 保留」）
        各自正确、互不衔接，所以旧实现把"全选"变成了**静默空操作**：
        PATCH 返回 200、UI 提示"已保存"，而库里 ``allowed_tools_json`` 仍是旧白名单，
        且一旦非 NULL 就再也回不到 NULL（ADR 的 I6 语义不可达）。

        这条用例走完"发 null ⇒ 应重置"：先收窄 → 再 ``null`` → 断言 GET 是 ``null``、
        **且**被挡的工具重新出现在注册表里。把路由改回 ``is not None`` ⇒ 本条红。
        """
        with tempfile.TemporaryDirectory() as directory:
            app, lifespan = await self._app(directory)
            try:
                container = app.state.container
                async with self._client(app) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "python",
                            "enabled": False,
                            "allowedTools": ["echo"],
                            "allowUnattended": True,
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    server_id = created.json()["server"]["id"]

                    # 同形种子：禁用态建服务器（不拉真子进程）+ 直挂工具桥 + 注册。
                    config = container.mcp_server_repository.get_server(server_id)
                    connection = _LiveConnection(
                        config=config,
                        session=None,
                        stop_event=asyncio.Event(),
                        failure_event=asyncio.Event(),
                        task=None,
                        tools=(_bridge("demo", "echo"), _bridge("demo", "sum")),
                        state="connected",
                    )
                    container.mcp_manager._connections[server_id] = connection
                    container.mcp_manager._replace_registry_tools()
                    self.assertEqual(
                        ["mcp__demo__echo"], _mcp_names(container.tool_registry)
                    )

                    # ← 前端"全选"实际发出去的请求体（review 的 jsdom 探针实测形态）。
                    reset = await client.patch(
                        f"/mcp/servers/{server_id}", json={"allowedTools": None}
                    )
                    self.assertEqual(200, reset.status_code)
                    self.assertIsNone(reset.json()["server"]["allowedTools"])

                    # GET 也如实（用户刷新后看到"全部工具"，而不是旧白名单）。
                    listed = await client.get("/mcp/servers")
                    self.assertEqual(200, listed.status_code)
                    by_name = {
                        item["name"]: item for item in listed.json()["items"]
                    }
                    self.assertIsNone(by_name["demo"]["allowedTools"])

                    # 库原始列回到 NULL，不是 ``[]``（三态可区分）。
                    raw_allowed, _raw_unattended = _raw_mcp_columns(
                        container.database, server_id
                    )
                    self.assertIsNone(raw_allowed)
                    self.assertIsNone(
                        container.mcp_server_repository.get_server(
                            server_id
                        ).allowed_tools
                    )

                    # 被挡的工具重新进注册表 = "回到全部允许"在**行为**上成立。
                    # （走路由的 PATCH 会触发 reload：禁用态服务器 ⇒ disconnect，把上面
                    #   手挂的连接取了下来。这里按"reload 成功"的形态把新配置贴回连接再
                    #   重注册，与 `test_widening_allowlist_brings_the_tool_back` 同法。）
                    reloaded = container.mcp_server_repository.get_server(server_id)
                    connection.config = reloaded
                    container.mcp_manager._connections[server_id] = connection
                    container.mcp_manager._replace_registry_tools()
                    self.assertEqual(
                        ["mcp__demo__echo", "mcp__demo__sum"],
                        _mcp_names(container.tool_registry),
                    )
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_patch_env_missing_keeps_secret_and_given_replaces_wholesale(self) -> None:
        """F1 复审交接的后端缺口：PATCH 缺 ``env`` = 保留；给了 = **整体替换**。

        前端侧 F1 已用键集合钉住，后端侧至今为零（``test_mcp_runtime.py:603`` 的 PATCH
        用例只带 ``enabled``）。本批恰好动了这段 coalesce，所以在这里补齐。
        """
        with tempfile.TemporaryDirectory() as directory:
            app, lifespan = await self._app(directory)
            try:
                container = app.state.container
                async with self._client(app) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "enabled": False,
                            "env": {"API_KEY": "secret-1", "OTHER": "keep"},
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    server_id = created.json()["server"]["id"]

                    # 缺 env（只改 name）⇒ 已存的两个 key 都保留。
                    renamed = await client.patch(
                        f"/mcp/servers/{server_id}", json={"name": "demo2"}
                    )
                    self.assertEqual(200, renamed.status_code)
                    stored = container.mcp_server_repository.get_server(server_id)
                    self.assertEqual(
                        {"API_KEY": "secret-1", "OTHER": "keep"}, dict(stored.env)
                    )

                    # 给 env（只给一个新 key）⇒ **整体替换**，老 key 全丢（I5 的已知限制，
                    # 做成显式断言而不是静默掉凭据）。
                    replaced = await client.patch(
                        f"/mcp/servers/{server_id}",
                        json={"env": {"API_KEY": "secret-2"}},
                    )
                    self.assertEqual(200, replaced.status_code)
                    stored = container.mcp_server_repository.get_server(server_id)
                    self.assertEqual({"API_KEY": "secret-2"}, dict(stored.env))
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_new_server_without_flags_is_unattended_disabled(self) -> None:
        """安全默认要证明**行为**（ADR §2.4）：不传 allowUnattended ⇒ 库里是 0。"""
        with tempfile.TemporaryDirectory() as directory:
            app, lifespan = await self._app(directory)
            try:
                async with self._client(app) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "enabled": False,
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    server_id = created.json()["server"]["id"]
                    self.assertFalse(created.json()["server"]["allowUnattended"])
                    _raw_allowed, raw_unattended = _raw_mcp_columns(
                        app.state.container.database, server_id
                    )
                    self.assertEqual(0, raw_unattended)
            finally:
                await lifespan.__aexit__(None, None, None)


class McpReloadFailureIsVisibleTest(unittest.IsolatedAsyncioTestCase):
    """§6 处置 (a)：三个 reload 调用点不再 `except Exception: pass`。

    改白名单/未值守之后 reload 若失败，`allowedTools`（新配置）与 `tools`（旧连接）
    会在同一次响应里自相矛盾，而用户以为已生效。这里让 `manager.reload` 抛错，
    断言三个路由都**如实**回报 `reloadError`（"已保存但未重新加载工具"）。
    """

    @asynccontextmanager
    async def _app(self, directory: str):
        app = create_app(
            settings=AppSettings(
                database_path=Path(directory) / "reload.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
            ),
            provider=None,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            yield app
        finally:
            await lifespan.__aexit__(None, None, None)

    @staticmethod
    def _break_reload(app) -> None:
        async def boom(server_id: str):
            raise RuntimeError("连接 MCP 服务器 demo 超时。")

        app.state.container.mcp_manager.reload = boom  # type: ignore[assignment]

    async def test_patch_reports_reload_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with self._app(directory) as app:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "enabled": False,
                        },
                    )
                    server_id = created.json()["server"]["id"]
                    self._break_reload(app)

                    patched = await client.patch(
                        f"/mcp/servers/{server_id}",
                        json={"allowedTools": ["echo"], "allowUnattended": True},
                    )
                    self.assertEqual(200, patched.status_code)
                    server = patched.json()["server"]
                    # 设置**确实存下来了**（两半不一致的形态就在这里）。
                    self.assertEqual(["echo"], server["allowedTools"])
                    self.assertTrue(server["allowUnattended"])
                    # 但重新加载失败必须可见，而不是被吞成"一切正常"。
                    self.assertIn("超时", server["reloadError"] or "")

    async def test_explicit_reload_route_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with self._app(directory) as app:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    created = await client.post(
                        "/mcp/servers",
                        json={
                            "name": "demo",
                            "transport": "stdio",
                            "command": "node",
                            "enabled": False,
                        },
                    )
                    server_id = created.json()["server"]["id"]
                    self._break_reload(app)
                    reloaded = await client.post(
                        f"/mcp/servers/{server_id}/reload"
                    )
                    self.assertEqual(200, reloaded.status_code)
                    self.assertIn(
                        "超时", reloaded.json()["server"]["reloadError"] or ""
                    )


class _ManagerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "manager.db")
        self.database.initialize()
        self.repository = SqliteMcpServerRepository(self.database)
        self.registry = ToolRegistry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self) -> McpManager:
        return McpManager(repository=self.repository, tool_registry=self.registry)

    def _connection(self, manager: McpManager, config, tools: tuple[McpToolBridge, ...]):
        connection = _LiveConnection(
            config=config,
            session=None,
            stop_event=asyncio.Event(),
            failure_event=asyncio.Event(),
            task=None,
            tools=tools,
            state="connected",
        )
        manager._connections[config.id] = connection
        return connection


class McpRegistrationFilterTest(_ManagerCase):
    """ADR 的 Critical-2：白名单必须在**注册那一步**排除，且两处都要。"""

    def _seed(self, *, allowed_tools, allow_unattended: bool = True):
        return self.repository.create_server(
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="python",
                allowed_tools=allowed_tools,
                allow_unattended=allow_unattended,
            )
        )

    async def test_replace_registry_tools_applies_allowlist(self) -> None:
        config = self._seed(allowed_tools=("echo",))
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )

        manager._replace_registry_tools()

        self.assertEqual(["mcp__demo__echo"], _mcp_names(self.registry))
        # 双向：允许的在注册表里、resolve 拿得到；被禁的**执行面**也拿不到。
        self.assertEqual(
            "mcp__demo__echo", self.registry.resolve("mcp__demo__echo").definition.name
        )
        with self.assertRaises(Exception):
            self.registry.resolve("mcp__demo__sum")

    async def test_registry_replace_with_applies_allowlist(self) -> None:
        config = self._seed(allowed_tools=("echo",))
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )

        manager._registry_replace_with({})

        self.assertEqual(["mcp__demo__echo"], _mcp_names(self.registry))
        with self.assertRaises(Exception):
            self.registry.resolve("mcp__demo__sum")

    async def test_empty_allowlist_disables_every_tool(self) -> None:
        config = self._seed(allowed_tools=())
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )

        manager._replace_registry_tools()

        self.assertEqual([], _mcp_names(self.registry))

    async def test_null_allowlist_keeps_every_tool(self) -> None:
        config = self._seed(allowed_tools=None)
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )

        manager._replace_registry_tools()

        self.assertEqual(
            ["mcp__demo__echo", "mcp__demo__sum"], _mcp_names(self.registry)
        )

    async def test_status_reports_allowed_marker_without_dropping_tools(self) -> None:
        """I2：被挡掉的工具**仍出现在 status().tools 里**，只是 ``allowed=False``。

        否则用户收窄白名单后就看不到被排除的工具、无法重新放行。
        """
        config = self._seed(allowed_tools=("echo",))
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )
        manager._replace_registry_tools()

        status = manager.status(config.id)
        self.assertEqual(2, status.tool_count)
        by_raw = {tool.raw_name: tool.allowed for tool in status.tools}
        self.assertEqual({"echo": True, "sum": False}, by_raw)

    async def test_widening_allowlist_brings_the_tool_back(self) -> None:
        config = self._seed(allowed_tools=("echo",))
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )
        manager._replace_registry_tools()
        self.assertEqual(["mcp__demo__echo"], _mcp_names(self.registry))

        widened = self.repository.update_server(
            config.id,
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="python",
                allowed_tools=("echo", "sum"),
                allow_unattended=True,
            ),
        )
        manager._connections[config.id].config = widened
        manager._replace_registry_tools()

        self.assertEqual(
            ["mcp__demo__echo", "mcp__demo__sum"], _mcp_names(self.registry)
        )

    async def test_allowlist_banned_tool_is_unattended_excluded_too(self) -> None:
        """**修复波 I3 / m6g**：排除集覆盖连接里枚举到的**全部**工具，不只白名单内的。

        ``allow_unattended=0`` 的服务器上，被白名单挡掉的 ``sum``（不在注册表里）
        也必须是"未值守不可用"——``_recompute_unattended_excluded`` 的 fail-safe 口径。
        把它改成"只记白名单内的工具"（``if tool_allowed(...): excluded.add(...)``）语义
        会**反转**成"被白名单禁掉的工具在未值守里反而放行"，本条即红。
        """
        config = self._seed(allowed_tools=("echo",), allow_unattended=False)
        manager = self._manager()
        self._connection(
            manager, config, (_bridge("demo", "echo"), _bridge("demo", "sum"))
        )
        manager._replace_registry_tools()

        # 白名单允许的（在注册表里）与白名单挡掉的（不在注册表里）都不可未值守。
        self.assertIs(False, manager.allows_unattended("mcp__demo__echo"))
        self.assertIs(False, manager.allows_unattended("mcp__demo__sum"))

        # 判别力：服务器打开未值守后两者都放行（不是"永远 False"）。
        opened = self.repository.update_server(
            config.id,
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="python",
                allowed_tools=("echo",),
                allow_unattended=True,
            ),
        )
        manager._connections[config.id].config = opened
        manager._recompute_unattended_excluded(manager._registry_tool_names())
        self.assertIs(True, manager.allows_unattended("mcp__demo__echo"))
        self.assertIs(True, manager.allows_unattended("mcp__demo__sum"))


class McpUnattendedCompatGateTest(_ManagerCase):
    """**修复波 I3 / m6e**：``allows_unattended`` 的兼容门只能在这里钉住。

    ``manager.py:595`` 的 ``if not self._saw_unattended_scope: return True`` 是"管理器
    从未见过 MCP 连接 ⇒ 与 F2 之前逐字节一致"的兼容分支。真实流程里
    ``_unattended_excluded`` 非空**必然**伴随 ``_saw_unattended_scope=True``
    （``_recompute_unattended_excluded`` 只在 ``self._connections`` 非空时置位），
    所以这条门在端到端路径上不可达 ⇒ 只能像下面这样**直接**摆出那个状态。
    把它改成 ``if False:`` ⇒ 本条红。
    """

    async def test_never_connected_manager_stays_compatible(self) -> None:
        manager = self._manager()
        self.assertFalse(
            manager._saw_unattended_scope, "新建管理器不该已经见过连接"
        )
        # 直接摆出"从未连接、却有一条排除记录"的状态（见类 docstring）。
        manager._unattended_excluded = frozenset({"mcp__demo__echo"})
        self.assertIs(True, manager.allows_unattended("mcp__demo__echo"))

        # 见过连接（真实路径）之后同一条排除记录必须生效——兼容门不是"永远放行"。
        config = self.repository.create_server(
            McpServerDraft(
                name="demo",
                transport="stdio",
                command="python",
                allowed_tools=None,
                allow_unattended=False,
            )
        )
        self._connection(manager, config, (_bridge("demo", "echo"),))
        manager._replace_registry_tools()
        self.assertTrue(manager._saw_unattended_scope)
        self.assertIs(False, manager.allows_unattended("mcp__demo__echo"))


def _production_settings(database_path: Path, mode: str) -> AppSettings:
    return AppSettings(
        database_path=database_path,
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        execution_backend_mode=mode,
    )


@asynccontextmanager
async def _production_app(database_path: Path, mode: str):
    app = create_app(settings=_production_settings(database_path, mode))
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client, app.state.container
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class McpUnattendedScopeTest(unittest.IsolatedAsyncioTestCase):
    """ADR 的 I1：``allow_unattended`` 要覆盖**三处**，且两种 execution backend 配置都要。

    种子做法与产品路径同形：**禁用态**建服务器（不拉起真子进程）→ 直接写入工具桥
    （模拟"连接已建立、工具已发现"）→ 用 ``_replace_registry_tools`` 注册。
    """

    async def _seed_registered(
        self,
        client,
        container,
        *,
        allowed_tools: list[str] | None,
        allow_unattended: bool,
    ):
        created = await client.post(
            "/mcp/servers",
            json={
                "name": "demo",
                "transport": "stdio",
                "command": "python",
                "enabled": False,
                "allowedTools": allowed_tools,
                "allowUnattended": allow_unattended,
            },
        )
        self.assertEqual(201, created.status_code)
        server_id = created.json()["server"]["id"]
        manager = container.mcp_manager
        config = container.mcp_server_repository.get_server(server_id)
        connection = _LiveConnection(
            config=config,
            session=None,
            stop_event=asyncio.Event(),
            failure_event=asyncio.Event(),
            task=None,
            tools=(_bridge("demo", "echo"),),
            state="connected",
        )
        manager._connections[server_id] = connection
        manager._replace_registry_tools()
        return server_id, connection

    async def _task_and_child_filters(self, container, conversation_id: str):
        unattended_filter = (
            container.task_worker._task_executor._tool_coordinator._tool_filter_provider
        )
        child_executor = container.delegation_handler._kernel_provider(None)._executor_builder(
            SimpleNamespace(deadline_monotonic=0.0)
        )
        child_filter = child_executor._tool_coordinator._tool_filter_provider
        return (
            unattended_filter(conversation_id),
            child_filter(conversation_id),
            child_executor,
        )

    async def _assert_configuration(self, mode: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / f"unattended-{mode or 'off'}.db"
            async with _production_app(database_path, mode) as (client, container):
                server_id, connection = await self._seed_registered(
                    client,
                    container,
                    allowed_tools=None,
                    allow_unattended=False,
                )
                tool_name = "mcp__demo__echo"
                conversation_id = (await create_bound_conversation(client))["id"]

                # 白名单允许 ⇒ 工具在注册表里（交互面可用）。
                self.assertIn(tool_name, _mcp_names(container.tool_registry))
                # 交互主路径的过滤器**不看**这个开关（否则"关掉未值守"会把交互也关掉）：
                # 交互面在网关装配处绑的是 `_v2_workspace_tool_filter`（只看工作区绑定），
                # 未值守开关只出现在任务/委派两处闭包里。
                wiring_source = inspect.getsource(_api_module.wiring)
                self.assertIn(
                    "tool_filter_provider=_v2_workspace_tool_filter", wiring_source
                )
                self.assertIn(
                    "tool_filter_provider=_v2_unattended_tool_filter",
                    wiring_source,
                )
                self.assertIn(
                    "tool_filter_provider=_delegation_child_tool_filter",
                    wiring_source,
                )
                unattended_allows, child_allows, child_executor = (
                    await self._task_and_child_filters(container, conversation_id)
                )
                # ① allow_unattended=0：任务运行 + 委派子运行的**模型面**都不可用。
                self.assertIs(
                    False,
                    unattended_allows(tool_name),
                    f"mode={mode!r}: 任务运行过滤器放行了 allow_unattended=0 的工具",
                )
                self.assertIs(
                    False,
                    child_allows(tool_name),
                    f"mode={mode!r}: 委派子运行过滤器放行了 allow_unattended=0 的工具",
                )
                # ② 执行面：协调器用**同一个谓词**挡。这是"两处配置都成立"的关键一层：
                #    默认配置下未值守执行器拿的是**裸 registry**，注册层与模型面过滤
                #    都不拦"绕过模型直接发起的调用"，协调器的 `_prepare_tool_batch`
                #    用同一个 `_tool_filter_provider` 在读工具时兜住。
                coordinator = (
                    container.task_worker._task_executor._tool_coordinator
                )
                self.assertIsNotNone(coordinator._tool_filter_provider)
                # 谓词**两个实例都是同一个闭包语义**（每条 run 现取一次）。
                self.assertIs(False, bool(coordinator._tool_filter_provider(conversation_id)(tool_name)))
                # 未值守执行器读到的是哪个注册表视图（证据，不做断言口径）：
                #   空配置 = 裸 `selected_tool_registry`；非空 = `EnforcingToolRegistry`。
                executor_registry = coordinator._tool_registry
                self.assertIs(
                    executor_registry is container.unattended_tool_registry,
                    bool(mode),
                    f"mode={mode!r}: 未值守执行器的注册表视图与配置不符",
                )
                # ③ 白名单外的工具在**注册表**里也不存在（执行面按名 resolve 拿不到）——
                #    由 McpRegistrationFilterTest 覆盖（同一注册路径）。

                # ④ 把开关打开 ⇒ 三处都放行（证明这一路是有判别力的，不是"永远 False"）。
                now_allowed = container.mcp_server_repository.update_server(
                    server_id,
                    McpServerDraft(
                        name="demo",
                        transport="stdio",
                        command="python",
                        allowed_tools=None,
                        allow_unattended=True,
                    ),
                )
                connection.config = now_allowed
                container.mcp_manager._replace_registry_tools()
                unattended_allows_2, child_allows_2, _ = (
                    await self._task_and_child_filters(container, conversation_id)
                )
                self.assertIs(True, unattended_allows_2(tool_name))
                self.assertIs(True, child_allows_2(tool_name))

    async def test_default_configuration(self) -> None:
        await self._assert_configuration("")

    async def test_execution_backend_configuration(self) -> None:
        await self._assert_configuration("local")


class McpCoordinatorExecutionGateTest(unittest.IsolatedAsyncioTestCase):
    """协调器执行面闸（ADR 的 I1 ③）：``_prepare_tool_batch`` 用**同一个**谓词读工具。

    这是"两种 execution backend 配置都真的挡得住"的那一层：默认配置下未值守执行器
    拿的是裸 registry，模型面过滤不拦执行。
    """

    async def test_forbidden_mcp_tool_is_not_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "gate.db"
            async with _production_app(database_path, "") as (client, container):
                created = await client.post(
                    "/mcp/servers",
                    json={
                        "name": "demo",
                        "transport": "stdio",
                        "command": "python",
                        "enabled": False,
                        "allowUnattended": False,
                    },
                )
                server_id = created.json()["server"]["id"]
                config = container.mcp_server_repository.get_server(server_id)
                connection = _LiveConnection(
                    config=config,
                    session=None,
                    stop_event=asyncio.Event(),
                    failure_event=asyncio.Event(),
                    task=None,
                    tools=(_bridge("demo", "echo"),),
                    state="connected",
                )
                container.mcp_manager._connections[server_id] = connection
                container.mcp_manager._replace_registry_tools()

                conversation_id = (await create_bound_conversation(client))["id"]
                coordinator = (
                    container.task_worker._task_executor._tool_coordinator
                )
                # 工具确实注册在**交互**注册表里（否则这条用例会因"名字不存在"而空转）。
                self.assertIn("mcp__demo__echo", _mcp_names(container.tool_registry))
                # 但未值守谓词不放行 ⇒ 协调器读到它时直接失败。
                predicate = coordinator._tool_filter_provider(conversation_id)
                self.assertIs(False, bool(predicate("mcp__demo__echo")))
                self.assertIs(
                    False,
                    container.mcp_manager.allows_unattended("mcp__demo__echo"),
                )
                # 打开开关后同一个谓词放行（证明闸门有判别力、不是"永远 False"）。
                updated = container.mcp_server_repository.update_server(
                    server_id,
                    McpServerDraft(
                        name="demo",
                        transport="stdio",
                        command="python",
                        allow_unattended=True,
                    ),
                )
                connection.config = updated
                container.mcp_manager._replace_registry_tools()
                predicate = coordinator._tool_filter_provider(conversation_id)
                self.assertIs(True, bool(predicate("mcp__demo__echo")))

    async def test_prepare_tool_batch_gate_blocks_the_forbidden_tool(self) -> None:
        """**修复波 I2**：执行面闸的**调用点**（``_prepare_tool_batch``）必须真的挡。

        旧覆盖（`test_execution_gate_reads_the_same_predicate`）只把**函数本体**与
        谓词对拍、**不经过** ``_prepare_tool_batch``：把 ``coordinator.py`` 里
        ``if not predicate_allows_tool(...)`` 那块换成 ``if False:`` 全套仍绿。
        按复审 I1，默认配置（``tool_platform_v2_enabled`` 默认 True）下 v2 未值守
        **模型面确实含** ``mcp__demo__echo`` ⇒ 这道闸在那条路径上是**唯一**的一道门，
        所以必须钉住它的调用点（这里走真仓储的 ``record_tool_call`` 造
        ``tool_executions`` 行，与 ``execute`` 同形）。

        M2：失败记录同时要**可区分且诚实**——不再是
        "模型提供的工具调用或参数不符合要求。"。
        """
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "prepare-gate.db"
            async with _production_app(database_path, "") as (client, container):
                created = await client.post(
                    "/mcp/servers",
                    json={
                        "name": "demo",
                        "transport": "stdio",
                        "command": "python",
                        "enabled": False,
                        "allowUnattended": False,
                    },
                )
                self.assertEqual(201, created.status_code)
                server_id = created.json()["server"]["id"]
                config = container.mcp_server_repository.get_server(server_id)
                connection = _LiveConnection(
                    config=config,
                    session=None,
                    stop_event=asyncio.Event(),
                    failure_event=asyncio.Event(),
                    task=None,
                    tools=(_bridge("demo", "echo"),),
                    state="connected",
                )
                container.mcp_manager._connections[server_id] = connection
                container.mcp_manager._replace_registry_tools()

                conversation_id = (await create_bound_conversation(client))["id"]
                coordinator = (
                    container.task_worker._task_executor._tool_coordinator
                )
                repository = container.runtime_v2_repository
                self.assertIn("mcp__demo__echo", _mcp_names(container.tool_registry))

                lane = repository.create_lane(conversation_id=conversation_id)
                trigger = repository.append_entry(
                    conversation_id=conversation_id,
                    lane_id=lane.id,
                    type=TranscriptEntryType.USER_MESSAGE,
                    actor=Actor.USER,
                    payload={"content": "跑一下 echo"},
                    context_policy={"include_in_llm": True, "transform": "full"},
                )
                run = repository.create_run(
                    conversation_id=conversation_id,
                    lane_id=lane.id,
                    trigger_entry_id=trigger.id,
                )
                model_turn = repository.create_model_turn(
                    run_id=run.id, provider="fake", model="fake-model"
                )

                def _item(call_id: str) -> _ToolWorkItem:
                    record, _entry = repository.record_tool_call(
                        model_turn_id=model_turn.id,
                        call_id=call_id,
                        tool_name="mcp__demo__echo",
                        arguments={},
                    )
                    return _ToolWorkItem(
                        provider_call=ProviderToolCall(
                            id=call_id, name="mcp__demo__echo", arguments={}
                        ),
                        record_id=record.id,
                    )

                # ⓘ I1（复审实测，本用例顺便钉住它的**理由**）：默认配置
                #   （`tool_platform_v2_enabled` 默认 True）下 v2 未值守**模型面确实含**
                #   这个工具——`definitions()` 走 `_v2_surface_definitions_provider`，
                #   它在每次调用里重建 catalog 且**不调用** `allows_unattended`
                #   （只剔 `exclude_tools` 与 `unattended and approval==REQUIRED`；
                #   `mcp__demo__echo` 是 read_only ⇒ AUTO ⇒ 不被剔）。
                #   ⇒ 执行面闸在这条路径上是**唯一**的一道门（不是"第二道"）。
                surface_names = {
                    definition.name
                    for definition in coordinator.definitions(conversation_id)
                }
                self.assertIn("mcp__demo__echo", surface_names)
                self.assertIs(
                    False,
                    bool(
                        coordinator._tool_filter_provider(conversation_id)(
                            "mcp__demo__echo"
                        )
                    ),
                )

                # ① ^ 服务器默认 allow_unattended=0 ⇒ 读工具那一步直接失败。
                blocked = _item("call_blocked")
                prepared = coordinator._prepare_tool_batch(
                    run=run,
                    model_turn=model_turn,
                    items=[blocked],
                    cancellation_token=CancellationToken(),
                )
                self.assertEqual([], prepared, "执行面闸没有挡住被禁的 MCP 工具")
                self.assertEqual(ToolExecutionStatus.FAILED, blocked.status)
                self.assertIsNotNone(blocked.error)
                self.assertEqual(
                    "tool_not_allowed_in_unattended_run", blocked.error.code
                )
                self.assertNotIn("不符合要求", blocked.error.safe_message)
                # F2 跟进波（复审 F-1）：文案**不承诺**一个具体设置——成因 ① 只是三种
                # 之一，这里同样不许出现"服务器没有开启"这类单因归因。
                self.assertNotIn("没有开启", blocked.error.safe_message)
                self.assertNotIn("允许未值守调用", blocked.error.safe_message)

                # ② 打开开关 ⇒ 同一条调用被准备（证明闸有条件，不是"永远失败"）。
                updated = container.mcp_server_repository.update_server(
                    server_id,
                    McpServerDraft(
                        name="demo",
                        transport="stdio",
                        command="python",
                        allow_unattended=True,
                    ),
                )
                connection.config = updated
                container.mcp_manager._replace_registry_tools()
                allowed = _item("call_allowed")
                prepared = coordinator._prepare_tool_batch(
                    run=run,
                    model_turn=model_turn,
                    items=[allowed],
                    cancellation_token=CancellationToken(),
                )
                self.assertEqual(1, len(prepared))
                self.assertIs(prepared[0].item, allowed)

    async def test_required_write_tool_message_does_not_promise_a_setting(self) -> None:
        """**F2 跟进波（复审 F-1）**：成因 ③ 下文案不得把人引向"改了也没用的设置"。

        闸的谓词（``api/wiring.py`` 的 ``_v2_unattended_tool_filter``）有三种 False
        成因：① 服务器没开 ``allowUnattended``（含不在白名单、连接已断）；② 工作区/
        能力过滤；③ 工具是 REQUIRED 审批——危险写类**永远**不允许无人值守。

        修复波的文案只归因 ①（"它的 MCP 服务器没有开启「允许未值守调用」。"），在 ③
        下**指向一个改了也没用的设置**：本用例里服务器 ``allowUnattended=True``、工具
        也在白名单里（``allows_unattended`` 为 True），但工具是**写类** ⇒ REQUIRED ⇒
        照样被闸挡住。所以文案必须**中性**（只陈述"未获准"，不暗示是哪一种设置）。

        这类工具不在未值守模型面（surface provider 会剔 REQUIRED），只在"绕过模型的
        直接调用/在飞调用"上出现——恰恰是这道闸存在的理由。
        """
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "required-write.db"
            async with _production_app(database_path, "") as (client, container):
                created = await client.post(
                    "/mcp/servers",
                    json={
                        "name": "demo",
                        "transport": "stdio",
                        "command": "python",
                        "enabled": False,
                        "allowUnattended": True,
                    },
                )
                self.assertEqual(201, created.status_code)
                server_id = created.json()["server"]["id"]
                config = container.mcp_server_repository.get_server(server_id)
                connection = _LiveConnection(
                    config=config,
                    session=None,
                    stop_event=asyncio.Event(),
                    failure_event=asyncio.Event(),
                    task=None,
                    tools=(_bridge("demo", "write", read_only=False),),
                    state="connected",
                )
                container.mcp_manager._connections[server_id] = connection
                container.mcp_manager._replace_registry_tools()

                conversation_id = (await create_bound_conversation(client))["id"]
                coordinator = (
                    container.task_worker._task_executor._tool_coordinator
                )
                repository = container.runtime_v2_repository

                # 前提逐一摆明，证明被挡的是**成因 ③**而不是 ①/②：
                self.assertIn("mcp__demo__write", _mcp_names(container.tool_registry))
                self.assertIs(
                    True,
                    container.mcp_manager.allows_unattended("mcp__demo__write"),
                    "服务器已开未值守 ⇒ 不是成因 ①",
                )
                self.assertIs(
                    ToolApprovalMode.REQUIRED,
                    container.tool_registry.resolve(
                        "mcp__demo__write"
                    ).definition.approval_mode,
                )
                predicate = coordinator._tool_filter_provider(conversation_id)
                self.assertIs(False, bool(predicate("mcp__demo__write")))

                lane = repository.create_lane(conversation_id=conversation_id)
                trigger = repository.append_entry(
                    conversation_id=conversation_id,
                    lane_id=lane.id,
                    type=TranscriptEntryType.USER_MESSAGE,
                    actor=Actor.USER,
                    payload={"content": "写一下"},
                    context_policy={"include_in_llm": True, "transform": "full"},
                )
                run = repository.create_run(
                    conversation_id=conversation_id,
                    lane_id=lane.id,
                    trigger_entry_id=trigger.id,
                )
                model_turn = repository.create_model_turn(
                    run_id=run.id, provider="fake", model="fake-model"
                )
                record, _entry = repository.record_tool_call(
                    model_turn_id=model_turn.id,
                    call_id="call_write",
                    tool_name="mcp__demo__write",
                    arguments={},
                )
                blocked = _ToolWorkItem(
                    provider_call=ProviderToolCall(
                        id="call_write", name="mcp__demo__write", arguments={}
                    ),
                    record_id=record.id,
                )
                prepared = coordinator._prepare_tool_batch(
                    run=run,
                    model_turn=model_turn,
                    items=[blocked],
                    cancellation_token=CancellationToken(),
                )
                self.assertEqual([], prepared, "执行面闸没有挡住 REQUIRED 写类 MCP 工具")
                self.assertEqual(ToolExecutionStatus.FAILED, blocked.status)
                self.assertIsNotNone(blocked.error)
                # 失败语义（code/retryable）是修复波定下、复审判对的——本波不动。
                self.assertEqual(
                    "tool_not_allowed_in_unattended_run", blocked.error.code
                )
                self.assertIs(False, blocked.error.retryable)
                # **本波的全部**：文案只陈述"未获准"，不再断言是哪一种设置——
                # 服务器**明明开着**未值守，说"没有开启"就是把人引向死路。
                message = blocked.error.safe_message
                self.assertIn("未获准", message, "文案仍要说清是「未获准」而不是别的原因")
                self.assertNotIn("没有开启", message)
                self.assertNotIn("允许未值守调用", message)

    async def test_execution_gate_reads_the_same_predicate(self) -> None:
        """闸门本体：``predicate_allows_tool`` 对**禁用的**工具返回 False、对允许的返回 True。

        ``_prepare_tool_batch`` 就是拿它的结果决定是否 ``raise ToolValidationError``
        （与注册表里"名字不存在"同一条失败语义），所以这一条能钉住执行面闸。
        """
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "gate2.db"
            async with _production_app(database_path, "") as (client, container):
                created = await client.post(
                    "/mcp/servers",
                    json={
                        "name": "demo",
                        "transport": "stdio",
                        "command": "python",
                        "enabled": False,
                        "allowUnattended": False,
                    },
                )
                server_id = created.json()["server"]["id"]
                config = container.mcp_server_repository.get_server(server_id)
                connection = _LiveConnection(
                    config=config,
                    session=None,
                    stop_event=asyncio.Event(),
                    failure_event=asyncio.Event(),
                    task=None,
                    tools=(_bridge("demo", "echo"),),
                    state="connected",
                )
                container.mcp_manager._connections[server_id] = connection
                container.mcp_manager._replace_registry_tools()
                conversation_id = (await create_bound_conversation(client))["id"]

                coordinator = (
                    container.task_worker._task_executor._tool_coordinator
                )
                predicate = coordinator._tool_filter_provider(conversation_id)
                # 闸本体在**模块级** `predicate_allows_tool`（不占冻结类的方法面），
                # `_prepare_tool_batch` 的读工具那一步直接调它。
                from endless_task.runtime_v2.execution_impl.coordinator import (
                    predicate_allows_tool,
                )

                # 禁用的 MCP 工具：执行面闸拒绝（与模型面同口径）。
                self.assertIs(
                    False,
                    predicate_allows_tool(predicate, "mcp__demo__echo"),
                )
                # 非 MCP 的内置工具不受这个开关影响（白名单/未值守只关乎 MCP）。
                self.assertIs(
                    True, predicate_allows_tool(predicate, "read_text_file")
                )
                # 没有谓词时放行（交互主路径）。
                self.assertIs(True, predicate_allows_tool(None, "mcp__demo__echo"))
                # 打开开关 ⇒ 放行（证明闸门有条件、不是常量）。
                updated = container.mcp_server_repository.update_server(
                    server_id,
                    McpServerDraft(
                        name="demo",
                        transport="stdio",
                        command="python",
                        allow_unattended=True,
                    ),
                )
                connection.config = updated
                container.mcp_manager._replace_registry_tools()
                predicate = coordinator._tool_filter_provider(conversation_id)
                self.assertIs(
                    True, predicate_allows_tool(predicate, "mcp__demo__echo")
                )


if __name__ == "__main__":
    unittest.main()
