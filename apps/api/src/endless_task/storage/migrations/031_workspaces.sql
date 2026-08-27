-- R5.11 工作区与知识分区。
-- 「通用」不是行，是 NULL：workspace_id IS NULL 即通用/全局语义，
-- 存量会话与源保持 NULL，迁移后行为不变。
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    root_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE conversations ADD COLUMN workspace_id TEXT
    REFERENCES workspaces(id);

ALTER TABLE knowledge_sources ADD COLUMN workspace_id TEXT
    REFERENCES workspaces(id);

CREATE INDEX idx_conversations_workspace
    ON conversations(workspace_id);
CREATE INDEX idx_knowledge_workspace
    ON knowledge_sources(workspace_id, status);
