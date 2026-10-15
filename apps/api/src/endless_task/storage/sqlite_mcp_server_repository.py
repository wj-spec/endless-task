"""R6.1 MCP 服务器配置仓储。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from endless_task.domain.repositories import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import new_id, utc_now

Clock = Callable[[], str]
IdFactory = Callable[[str], str]

MAX_NAME_CHARS = 32
MAX_TIMEOUT_SECONDS = 600.0


def parse_allowed_tools(raw: object) -> Optional[tuple[str, ...]]:
    """把 ``allowed_tools_json`` 的**原始列值**解析成白名单（批次 F Task 2）。

    语义（ADR §7 的 I6 裁定）——三态，必须显式区分：
      * ``None``（SQL NULL）/ ``""``（空串）/ 非 JSON 的**损坏值** ⇒ ``None`` = 未配置 = **全部允许**；
      * ``[]`` ⇒ ``()`` = **全部禁用**；
      * 非空数组 ⇒ 只允许列出的 ``rawName``。

    容错口径：**不**沿用本文件既有的 ``json.loads(row[...] or "[]")`` 风格——空串会抛
    ``JSONDecodeError``。这里把空串/损坏值按"未配置"（= 保持今天的行为）处理，宁可宽也不
    因一行坏 JSON 让整个服务器不可用；这是有意的宽松，理由写在 F2 报告里。
    """
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    if decoded is None:
        return None
    if not isinstance(decoded, (list, tuple)):
        return None
    return tuple(item for item in decoded if isinstance(item, str))


def dump_allowed_tools(allowed: Optional[tuple[str, ...]]) -> Optional[str]:
    """白名单落库形态：``None`` 存 SQL NULL；``()`` 存 ``"[]"``（= 全部禁用）。"""
    if allowed is None:
        return None
    return json.dumps(list(allowed), ensure_ascii=False)


@dataclass(frozen=True)
class McpServerConfig:
    id: str
    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    url: str
    headers: dict[str, str]
    enabled: bool
    tool_call_timeout_seconds: float
    created_at: str
    updated_at: str
    #: F2：``None`` = 未配置 = 全部允许；``()`` = 全部禁用；非空 = 只允许这些 rawName。
    allowed_tools: Optional[tuple[str, ...]] = None
    #: F2：该服务器的工具能否进**未值守**（定时任务 / 委派子运行）。列默认 0（fail-safe）。
    allow_unattended: bool = False


@dataclass(frozen=True)
class McpServerDraft:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: Optional[dict[str, str]] = None
    cwd: str = ""
    url: str = ""
    headers: Optional[dict[str, str]] = None
    enabled: bool = True
    tool_call_timeout_seconds: float = 60.0
    allowed_tools: Optional[tuple[str, ...]] = None
    allow_unattended: bool = False


class SqliteMcpServerRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def list_servers(self, *, enabled_only: bool = False) -> tuple[McpServerConfig, ...]:
        query = "SELECT * FROM mcp_servers"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name COLLATE NOCASE"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(self._config(row) for row in rows)

    def get_server(self, server_id: str) -> McpServerConfig:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"MCP server not found: {server_id}")
        return self._config(row)

    def create_server(self, draft: McpServerDraft) -> McpServerConfig:
        values = self._validated_values(draft)
        server_id = self._id_factory("mcp")
        now = self._clock()
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM mcp_servers WHERE name = ?", (values["name"],)
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"An MCP server named {values['name']!r} already exists."
                )
            connection.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, args_json, env_json, cwd,
                    url, headers_json, enabled, tool_call_timeout_seconds,
                    allowed_tools_json, allow_unattended,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server_id,
                    values["name"],
                    values["transport"],
                    values["command"],
                    json.dumps(values["args"], ensure_ascii=False),
                    json.dumps(values["env"], ensure_ascii=False),
                    values["cwd"],
                    values["url"],
                    json.dumps(values["headers"], ensure_ascii=False),
                    1 if values["enabled"] else 0,
                    values["timeout"],
                    dump_allowed_tools(values["allowed_tools"]),
                    1 if values["allow_unattended"] else 0,
                    now,
                    now,
                ),
            )
        return self.get_server(server_id)

    def update_server(
        self, server_id: str, draft: McpServerDraft
    ) -> McpServerConfig:
        values = self._validated_values(draft)
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"MCP server not found: {server_id}")
            duplicate = connection.execute(
                "SELECT id FROM mcp_servers WHERE name = ? AND id != ?",
                (values["name"], server_id),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    f"An MCP server named {values['name']!r} already exists."
                )
            connection.execute(
                """
                UPDATE mcp_servers SET
                    name = ?, transport = ?, command = ?, args_json = ?,
                    env_json = ?, cwd = ?, url = ?, headers_json = ?,
                    enabled = ?, tool_call_timeout_seconds = ?,
                    allowed_tools_json = ?, allow_unattended = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["name"],
                    values["transport"],
                    values["command"],
                    json.dumps(values["args"], ensure_ascii=False),
                    json.dumps(values["env"], ensure_ascii=False),
                    values["cwd"],
                    values["url"],
                    json.dumps(values["headers"], ensure_ascii=False),
                    1 if values["enabled"] else 0,
                    values["timeout"],
                    dump_allowed_tools(values["allowed_tools"]),
                    1 if values["allow_unattended"] else 0,
                    now,
                    server_id,
                ),
            )
        return self.get_server(server_id)

    def delete_server(self, server_id: str) -> None:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM mcp_servers WHERE id = ?", (server_id,)
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"MCP server not found: {server_id}")

    def _validated_values(self, draft: McpServerDraft) -> dict[str, object]:
        name = (draft.name or "").strip()
        if not name or len(name) > MAX_NAME_CHARS:
            raise ValidationError("MCP server name must contain 1-32 characters.")
        if any(
            not ("a" <= char <= "z" or "0" <= char <= "9" or char in ("-", "_"))
            for char in name
        ):
            raise ValidationError(
                "MCP server name can only contain lowercase letters, numbers, '-' and '_'."
            )
        if draft.transport not in ("stdio", "http"):
            raise ValidationError("MCP transport must be stdio or http.")
        if draft.transport == "stdio":
            command = (draft.command or "").strip()
            if not command:
                raise ValidationError("MCP stdio command is required.")
            args = tuple(draft.args)
            if any(not isinstance(item, str) for item in args):
                raise ValidationError("MCP args must be text values.")
            url = ""
            headers: dict[str, str] = {}
        else:
            command = ""
            args: tuple[str, ...] = ()
            url = (draft.url or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValidationError("MCP HTTP URL must start with http:// or https://.")
            headers = dict(draft.headers or {})
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
                raise ValidationError("MCP headers must be text values.")
        timeout = float(draft.tool_call_timeout_seconds)
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValidationError("MCP tool timeout must be between 1 and 600 seconds.")
        env = dict(draft.env or {})
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ValidationError("MCP environment values must be text.")
        # F2：白名单。None = 未配置（全部允许）；() = 全部禁用；非空 = 只允许列出的 rawName。
        if draft.allowed_tools is None:
            allowed_tools: Optional[tuple[str, ...]] = None
        else:
            allowed_tools = tuple(draft.allowed_tools)
            if any(not isinstance(item, str) for item in allowed_tools):
                raise ValidationError("MCP allowed tool names must be text values.")
            if any(not item.strip() for item in allowed_tools):
                raise ValidationError("MCP allowed tool names must not be empty.")
            if len(set(allowed_tools)) != len(allowed_tools):
                raise ValidationError("MCP allowed tool names must be unique.")
        return {
            "name": name,
            "transport": draft.transport,
            "command": command,
            "args": args,
            "env": env,
            "cwd": (draft.cwd or "").strip(),
            "url": url,
            "headers": headers,
            "enabled": bool(draft.enabled),
            "timeout": timeout,
            "allowed_tools": allowed_tools,
            "allow_unattended": bool(draft.allow_unattended),
        }

    def _config(self, row) -> McpServerConfig:
        return McpServerConfig(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            command=row["command"],
            args=tuple(json.loads(row["args_json"] or "[]")),
            env=dict(json.loads(row["env_json"] or "{}")),
            cwd=row["cwd"],
            url=row["url"],
            headers=dict(json.loads(row["headers_json"] or "{}")),
            enabled=bool(row["enabled"]),
            tool_call_timeout_seconds=float(row["tool_call_timeout_seconds"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            allowed_tools=parse_allowed_tools(row["allowed_tools_json"]),
            allow_unattended=bool(row["allow_unattended"]),
        )
