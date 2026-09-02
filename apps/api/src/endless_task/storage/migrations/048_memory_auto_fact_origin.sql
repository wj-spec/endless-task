-- 048_memory_auto_fact_origin.sql
-- 扩展 memories.write_origin 枚举,支持自动提取的 fact 记忆(origin=auto_fact)。
-- SQLite 无法修改 CHECK 约束,采用"建新表 → 复制 → 换名"的标准重建流程。
-- 保留 006/008 以来的全部列;memory_proposals 不引用 memories,重建无外键障碍。

CREATE TABLE memories_new (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'fact')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'deleted')),
    source_conversation_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    write_origin TEXT NOT NULL
        CHECK (write_origin IN ('confirmed_proposal', 'auto_fact')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expired_at TEXT,
    deleted_at TEXT,
    source_proposal_id TEXT,
    expired_reason TEXT,
    superseded_by TEXT,
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

INSERT INTO memories_new (
    id, kind, content, status,
    source_conversation_id, source_turn_id, write_origin,
    created_at, updated_at, expired_at, deleted_at,
    source_proposal_id, expired_reason, superseded_by
)
SELECT
    id, kind, content, status,
    source_conversation_id, source_turn_id, write_origin,
    created_at, updated_at, expired_at, deleted_at,
    source_proposal_id, expired_reason, superseded_by
FROM memories;

DROP TABLE memories;

ALTER TABLE memories_new RENAME TO memories;

CREATE INDEX idx_memories_status_updated ON memories(status, updated_at);
