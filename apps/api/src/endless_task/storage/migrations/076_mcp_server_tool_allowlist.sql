-- 076_mcp_server_tool_allowlist.sql
-- 批次 F Task 2（F2）：每服务器工具白名单 + 未值守边界。
--
-- 两列（纯 ADD COLUMN，零重建表；先例 020 / 024 / 062 / 069）：
--   * `allowed_tools_json`：可空。语义（ADR §7 的 I6 裁定）为
--       NULL / 空串 = **未配置** = 全部允许（保持今天的行为，存量行为零变化）；
--       `[]`        = **全部禁用**（表达"这个服务器一个工具都不给"）；
--       非空数组    = 只允许列出的**原始工具名**（`rawName`，不含 `mcp__<server>__` 前缀，
--                     见 mcp_runtime/naming.py；因此服务器改名不影响白名单）。
--   * `allow_unattended`：是否允许**未值守**（定时任务 / 委派子运行）调用该服务器的工具。
--
-- 默认值方向（ADR §2.4「先窄后宽」）：
--   * **既有行回填 1**——迁移前 MCP 工具事实上就能进未值守执行器，回填保持"零默认值变化"；
--   * **之后新建的行**走列默认 0（fail-safe）：用户显式打开才进未值守。
--   两条语句在同一个 `executescript`（BEGIN IMMEDIATE … COMMIT）里执行，见
--   storage/database.py:_migrate，所以"加列 → 回填"是原子的、且只对**真实存在**的行生效。

ALTER TABLE mcp_servers ADD COLUMN allowed_tools_json TEXT;
ALTER TABLE mcp_servers ADD COLUMN allow_unattended INTEGER NOT NULL DEFAULT 0;

-- 回填**既有**行（此刻表里只有迁移前就已存在的行；新插入的行走列默认 0）。
UPDATE mcp_servers SET allow_unattended = 1;
