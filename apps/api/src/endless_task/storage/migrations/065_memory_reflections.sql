-- 065_memory_reflections.sql
-- B4 反思：记录"从哪次失败里得出了哪条洞见"，并保证同一教训只提一次。
--   signature     -> 情景证据集合的稳定签名（UNIQUE）
--   source_refs   -> JSON 数组：run/工具执行/事件的引用，供溯源
--   proposal_id   -> 走既有记忆提案确认流（用户可拒绝）
--   insight_memory_id -> 确认后写入的语义记忆 id

CREATE TABLE memory_reflections (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    run_id TEXT,
    trigger TEXT NOT NULL,
    signature TEXT NOT NULL UNIQUE,
    insight_content TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    insight_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_memory_reflections_status
    ON memory_reflections(status, created_at DESC);
CREATE INDEX idx_memory_reflections_proposal
    ON memory_reflections(proposal_id);
