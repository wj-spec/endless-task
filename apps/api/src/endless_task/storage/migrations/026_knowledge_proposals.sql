CREATE TABLE knowledge_proposals (
    id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL
        CHECK (proposal_type IN ('add_source', 'expire_source')),
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

CREATE INDEX idx_knowledge_proposals_conversation
    ON knowledge_proposals(conversation_id, status);
