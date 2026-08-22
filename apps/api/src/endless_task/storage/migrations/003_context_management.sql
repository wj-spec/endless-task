CREATE TABLE conversation_summary_revisions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    through_turn_ordinal INTEGER NOT NULL CHECK (through_turn_ordinal >= 1),
    prompt_version TEXT NOT NULL,
    content TEXT NOT NULL,
    input_token_estimate INTEGER NOT NULL CHECK (input_token_estimate >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    UNIQUE (conversation_id, through_turn_ordinal, prompt_version)
);

CREATE INDEX idx_summary_revisions_conversation_ordinal
    ON conversation_summary_revisions(conversation_id, through_turn_ordinal DESC);
