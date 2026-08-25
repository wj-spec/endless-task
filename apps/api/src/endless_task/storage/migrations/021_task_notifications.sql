CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN
        ('run_completed', 'run_failed', 'run_awaiting')),
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_notifications_unread
    ON notifications(read_at, created_at DESC);
