CREATE TABLE task_proposals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    commitment TEXT NOT NULL CHECK (length(trim(commitment)) > 0),
    schedule TEXT NOT NULL CHECK (json_valid(schedule)),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_task_id TEXT,
    resolved_at TEXT,
    CHECK ((status IN ('accepted', 'rejected', 'cancelled')) = (resolved_at IS NOT NULL))
);

CREATE INDEX idx_task_proposals_conversation_status
    ON task_proposals(conversation_id, status);
