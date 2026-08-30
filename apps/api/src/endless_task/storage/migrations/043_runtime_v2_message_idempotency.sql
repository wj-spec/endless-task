-- Runtime v2 message idempotency: one client request maps to one message and run.
CREATE TABLE v2_message_requests (
    conversation_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    user_entry_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, client_request_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (user_entry_id) REFERENCES v2_transcript_entries(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES v2_runs(id) ON DELETE CASCADE
);

CREATE INDEX idx_v2_message_requests_run
    ON v2_message_requests(run_id);