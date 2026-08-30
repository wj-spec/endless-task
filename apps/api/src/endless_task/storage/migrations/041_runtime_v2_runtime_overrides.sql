CREATE TABLE v2_conversation_runtime_overrides (
    conversation_id TEXT PRIMARY KEY,
    runtime TEXT NOT NULL CHECK (runtime IN ('v1', 'v2')),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
