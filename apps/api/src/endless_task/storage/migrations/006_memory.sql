CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'fact')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'deleted')),
    source_conversation_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    write_origin TEXT NOT NULL CHECK (write_origin IN ('confirmed_proposal')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expired_at TEXT,
    deleted_at TEXT,
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX idx_memories_status_updated ON memories(status, updated_at);
