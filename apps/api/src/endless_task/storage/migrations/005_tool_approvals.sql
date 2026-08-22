CREATE TABLE tool_calls (
    turn_id TEXT NOT NULL,
    id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    response_variant_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    effect TEXT NOT NULL CHECK (
        effect IN ('read_only', 'local_write', 'external_action')
    ),
    approval_mode TEXT NOT NULL CHECK (approval_mode IN ('auto', 'required')),
    status TEXT NOT NULL CHECK (
        status IN (
            'created', 'waiting_approval', 'running',
            'completed', 'failed', 'cancelled'
        )
    ),
    result_truncated INTEGER NOT NULL DEFAULT 0 CHECK (result_truncated IN (0, 1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (turn_id, id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    FOREIGN KEY (response_variant_id) REFERENCES response_variants(id) ON DELETE CASCADE
);

CREATE INDEX idx_tool_calls_variant_created
    ON tool_calls(response_variant_id, created_at, id);

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'denied', 'cancelled', 'expired')
    ),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (turn_id, tool_call_id)
        REFERENCES tool_calls(turn_id, id) ON DELETE CASCADE,
    UNIQUE (turn_id, tool_call_id),
    CHECK (
        (status = 'pending' AND resolved_at IS NULL)
        OR (status != 'pending' AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX idx_approval_requests_turn_status
    ON approval_requests(turn_id, status, created_at);
