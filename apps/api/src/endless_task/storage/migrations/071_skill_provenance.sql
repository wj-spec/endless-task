-- 071_skill_provenance.sql
-- S8 生态安装来源记录：装进来的技能必须能追溯"从哪来、什么时候、扫描结论如何"。
--   * 主键 (scope, workspace_id, name)：同名技能只保留最新一次安装来源；
--   * digest 记录安装时的内容摘要，便于判断是否被后续升级覆盖；
--   * worst_level 记录安装时的扫描最高风险等级（low/warning/high/critical）。

CREATE TABLE IF NOT EXISTS skill_provenance (
    scope TEXT NOT NULL CHECK (scope IN ('user', 'workspace')),
    workspace_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    spec TEXT NOT NULL,
    source TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT 'HEAD',
    digest TEXT NOT NULL,
    worst_level TEXT,
    installed_at TEXT NOT NULL,
    PRIMARY KEY (scope, workspace_id, name)
);
