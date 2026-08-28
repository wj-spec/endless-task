-- R6.0 Skill 运行时：用户启用/禁用覆盖（扫描结果本身不落库）。
CREATE TABLE IF NOT EXISTS skill_overrides (
    scope TEXT NOT NULL CHECK (scope IN ('user', 'workspace')),
    workspace_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, workspace_id, name)
);
