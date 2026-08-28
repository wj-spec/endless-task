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
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    enabled = ?, tool_call_timeout_seconds = ?, updated_at = ?
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
        )
