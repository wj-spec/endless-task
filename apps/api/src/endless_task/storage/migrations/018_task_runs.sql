CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled')),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    conversation_id TEXT NOT NULL,
    turn_id TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    CHECK ((status <> 'running') = (finished_at IS NOT NULL))
);

CREATE INDEX idx_task_runs_task ON task_runs(task_id, started_at);
