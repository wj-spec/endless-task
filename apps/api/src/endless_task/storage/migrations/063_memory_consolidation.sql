-- 063_memory_consolidation.sql
-- B2 记忆巩固：记录"哪些记忆被合并成了哪条高层记忆"。
-- 一行 = 一次巩固候选（一个聚类）：
--   proposal_id   -> memory_proposals.id（走既有确认流，避免误合并）
--   signature     -> 聚类签名（UNIQUE，保证同样的记忆集合只并入一次）
--   source_memory_ids -> JSON 数组，保留溯源（原记忆标记为已并入，不删除）
--   insight_memory_id -> 确认后生成的洞察记忆 id

CREATE TABLE memory_consolidations (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    signature TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'fact')),
    source_memory_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    insight_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_memory_consolidations_status
    ON memory_consolidations(status);
CREATE INDEX idx_memory_consolidations_proposal
    ON memory_consolidations(proposal_id);
