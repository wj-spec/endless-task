-- R6.1 MCP 客户端：外部 MCP 服务器配置。
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL CHECK (transport IN ('stdio', 'http')),
    command TEXT NOT NULL DEFAULT '',
    args_json TEXT NOT NULL DEFAULT '[]',
    env_json TEXT NOT NULL DEFAULT '{}',
    cwd TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    headers_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    tool_call_timeout_seconds REAL NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
