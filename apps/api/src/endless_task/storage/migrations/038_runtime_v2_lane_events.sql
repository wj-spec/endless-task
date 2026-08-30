CREATE TABLE v2_lane_events (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    UNIQUE (conversation_id, event_seq)
);

CREATE INDEX idx_v2_lane_events_conversation_sequence
    ON v2_lane_events(conversation_id, event_seq);

CREATE INDEX idx_v2_lane_events_lane
    ON v2_lane_events(lane_id);
