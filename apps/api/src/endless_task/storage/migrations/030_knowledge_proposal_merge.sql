-- R5.10：knowledge_proposals.proposal_type 增加 merge_source。
-- SQLite 无法原地修改 CHECK 约束，按惯例重建表并回迁数据。
CREATE TABLE knowledge_proposals_v2 (
    id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL
        CHECK (proposal_type IN ('add_source', 'expire_source', 'merge_source')),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_source_id TEXT,
    CHECK ((status = 'pending') = (resolved_at IS NULL))
);

INSERT INTO knowledge_proposals_v2 (
    id, proposal_type, payload, status, conversation_id, turn_id,
    created_at, updated_at, resolved_at, resolved_source_id
)
SELECT id, proposal_type, payload, status, conversation_id, turn_id,
       created_at, updated_at, resolved_at, resolved_source_id
FROM knowledge_proposals;

DROP TABLE knowledge_proposals;

ALTER TABLE knowledge_proposals_v2 RENAME TO knowledge_proposals;

CREATE INDEX idx_knowledge_proposals_conversation
    ON knowledge_proposals(conversation_id, status);
