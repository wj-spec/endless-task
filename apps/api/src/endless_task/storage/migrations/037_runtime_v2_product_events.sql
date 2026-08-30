CREATE TABLE v2_product_events (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_type TEXT NOT NULL,
    run_id TEXT,
    lane_id TEXT,
    source_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES v2_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE SET NULL,
    UNIQUE (conversation_id, event_seq),
    UNIQUE (source_event_id)
);

CREATE INDEX idx_v2_product_events_conversation_sequence
    ON v2_product_events(conversation_id, event_seq);

CREATE INDEX idx_v2_product_events_run
    ON v2_product_events(run_id);
