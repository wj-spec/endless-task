CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    commitment TEXT NOT NULL CHECK (length(trim(commitment)) > 0),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'fired', 'cancelled')),
    source_conversation_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    source_proposal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fired_at TEXT,
    cancelled_at TEXT,
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    CHECK ((status = 'fired') = (fired_at IS NOT NULL))
);

CREATE INDEX idx_reminders_pending ON reminders(status, due_at);
