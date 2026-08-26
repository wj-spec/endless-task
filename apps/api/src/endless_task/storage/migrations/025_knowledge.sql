-- P5 个人知识与信息源：知识源实体
CREATE TABLE knowledge_sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'note')),
    origin TEXT NOT NULL CHECK (origin IN ('user', 'agent')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    content TEXT NOT NULL,
    file_name TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'deleted')),
    source_conversation_id TEXT,
    proposed_by_turn_id TEXT,
    user_edited_at TEXT,
    expires_at TEXT,
    expired_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX idx_knowledge_status_updated
    ON knowledge_sources(status, updated_at);
