CREATE TABLE v2_lanes (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('main', 'persistent_branch', 'temporary', 'archived')
    ),
    base_entry_id TEXT,
    leaf_entry_id TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_v2_lanes_one_main_per_conversation
    ON v2_lanes(conversation_id)
    WHERE kind = 'main';

CREATE INDEX idx_v2_lanes_conversation_kind
    ON v2_lanes(conversation_id, kind, created_at);

CREATE TABLE v2_transcript_entries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_id TEXT,
    lane_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    type TEXT NOT NULL,
    type_version INTEGER NOT NULL CHECK (type_version >= 1),
    actor TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('streaming', 'final', 'failed', 'cancelled')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    context_policy_json TEXT NOT NULL,
    display_json TEXT NOT NULL DEFAULT '{}',
    source_run_id TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES v2_transcript_entries(id),
    FOREIGN KEY (source_run_id) REFERENCES v2_runs(id),
    UNIQUE (lane_id, seq)
);

CREATE INDEX idx_v2_transcript_entries_conversation_type
    ON v2_transcript_entries(conversation_id, type, created_at);

CREATE INDEX idx_v2_transcript_entries_parent
    ON v2_transcript_entries(parent_id);

CREATE TABLE v2_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    trigger_entry_id TEXT NOT NULL,
    sibling_group_id TEXT NOT NULL,
    assistant_entry_id TEXT,
    is_active_variant INTEGER NOT NULL DEFAULT 0 CHECK (is_active_variant IN (0, 1)),
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'queued',
            'running',
            'waiting_approval',
            'compacting',
            'cancelling',
            'cancelled',
            'failed',
            'completed'
        )
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cancelled_by TEXT,
    error_code TEXT,
    safe_message TEXT,
    correlation_id TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (trigger_entry_id) REFERENCES v2_transcript_entries(id),
    FOREIGN KEY (assistant_entry_id) REFERENCES v2_transcript_entries(id)
);

CREATE UNIQUE INDEX idx_v2_runs_one_active_per_lane
    ON v2_runs(lane_id)
    WHERE status IN (
        'created', 'queued', 'running', 'waiting_approval', 'compacting', 'cancelling'
    );

CREATE INDEX idx_v2_runs_conversation_created
    ON v2_runs(conversation_id, created_at);

CREATE INDEX idx_v2_runs_sibling_group
    ON v2_runs(sibling_group_id, created_at);

CREATE UNIQUE INDEX idx_v2_runs_one_active_variant_per_sibling_group
    ON v2_runs(sibling_group_id)
    WHERE is_active_variant = 1;

CREATE TABLE v2_conversation_pointers (
    conversation_id TEXT PRIMARY KEY,
    active_lane_id TEXT NOT NULL,
    active_run_id TEXT,
    active_run_variant_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (active_lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (active_run_id) REFERENCES v2_runs(id) ON DELETE SET NULL,
    FOREIGN KEY (active_run_variant_id) REFERENCES v2_runs(id) ON DELETE SET NULL
);

CREATE INDEX idx_v2_conversation_pointers_lane
    ON v2_conversation_pointers(active_lane_id);

CREATE TABLE v2_model_turns (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL CHECK (turn_index >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'projecting_context',
            'waiting_provider_slot',
            'streaming',
            'executing_tools',
            'completed',
            'failed',
            'cancelled'
        )
    ),
    provider TEXT,
    model TEXT,
    request_id TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    safe_message TEXT,
    FOREIGN KEY (run_id) REFERENCES v2_runs(id) ON DELETE CASCADE,
    UNIQUE (run_id, turn_index)
);

CREATE INDEX idx_v2_model_turns_run_status
    ON v2_model_turns(run_id, status);

CREATE TABLE v2_tool_executions (
    id TEXT PRIMARY KEY,
    model_turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'validating',
            'waiting_approval',
            'running',
            'completed',
            'failed',
            'cancelled',
            'rejected',
            'expired'
        )
    ),
    approval_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    safe_message TEXT,
    result_entry_id TEXT,
    FOREIGN KEY (model_turn_id) REFERENCES v2_model_turns(id) ON DELETE CASCADE,
    FOREIGN KEY (result_entry_id) REFERENCES v2_transcript_entries(id),
    UNIQUE (model_turn_id, call_id)
);

CREATE INDEX idx_v2_tool_executions_turn_status
    ON v2_tool_executions(model_turn_id, status);

CREATE TABLE v2_runtime_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    model_turn_id TEXT,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES v2_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (model_turn_id) REFERENCES v2_model_turns(id) ON DELETE CASCADE,
    UNIQUE (run_id, event_seq)
);

CREATE INDEX idx_v2_runtime_events_run_sequence
    ON v2_runtime_events(run_id, event_seq);

CREATE INDEX idx_v2_runtime_events_type
    ON v2_runtime_events(event_type, occurred_at);

CREATE TABLE v2_context_compactions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    base_entry_id TEXT NOT NULL,
    summary_entry_id TEXT NOT NULL,
    covered_entry_ids_json TEXT NOT NULL,
    tokens_before INTEGER NOT NULL CHECK (tokens_before >= 0),
    tokens_after INTEGER NOT NULL CHECK (tokens_after >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (base_entry_id) REFERENCES v2_transcript_entries(id),
    FOREIGN KEY (summary_entry_id) REFERENCES v2_transcript_entries(id)
);

CREATE INDEX idx_v2_context_compactions_lane
    ON v2_context_compactions(lane_id, created_at);

CREATE TABLE v2_migration_state (
    migration_name TEXT PRIMARY KEY,
    migrated_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
