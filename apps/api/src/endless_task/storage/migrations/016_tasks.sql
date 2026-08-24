CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    commitment TEXT NOT NULL CHECK (length(trim(commitment)) > 0),
    schedule TEXT NOT NULL CHECK (json_valid(schedule)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'cancelled')),
    source_conversation_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    source_proposal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);
