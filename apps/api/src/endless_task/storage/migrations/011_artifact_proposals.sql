CREATE TABLE artifact_proposals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('markdown', 'text')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
    resolved_artifact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK ((status <> 'pending') = (resolved_at IS NOT NULL)),
    CHECK ((status = 'accepted') = (resolved_artifact_id IS NOT NULL))
);

CREATE INDEX idx_artifact_proposals_conversation_status
    ON artifact_proposals(conversation_id, status);
