CREATE TABLE v2_runtime_memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (
        scope IN (
            'user_global', 'workspace', 'conversation_tree',
            'branch', 'temporary', 'run_scratch'
        )
    ),
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'fact')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    conversation_id TEXT NOT NULL,
    workspace_id TEXT,
    lane_id TEXT,
    run_id TEXT,
    source_memory_id TEXT,
    source_entry_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES v2_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES v2_runtime_memories(id) ON DELETE SET NULL,
    FOREIGN KEY (source_entry_id) REFERENCES v2_transcript_entries(id) ON DELETE SET NULL
);

CREATE INDEX idx_v2_runtime_memories_visible
    ON v2_runtime_memories(status, conversation_id, scope, lane_id, run_id);

CREATE INDEX idx_v2_runtime_memories_source
    ON v2_runtime_memories(source_memory_id);

CREATE TABLE v2_memory_promotions (
    id TEXT PRIMARY KEY,
    source_memory_id TEXT NOT NULL,
    target_scope TEXT NOT NULL CHECK (
        target_scope IN ('workspace', 'conversation_tree', 'branch', 'user_global')
    ),
    target_workspace_id TEXT,
    target_lane_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_memory_id TEXT,
    conflict_memory_id TEXT,
    resolved_at TEXT,
    FOREIGN KEY (source_memory_id) REFERENCES v2_runtime_memories(id) ON DELETE CASCADE,
    FOREIGN KEY (target_workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL,
    FOREIGN KEY (target_lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (resolved_memory_id) REFERENCES v2_runtime_memories(id) ON DELETE SET NULL,
    FOREIGN KEY (conflict_memory_id) REFERENCES v2_runtime_memories(id) ON DELETE SET NULL,
    CHECK ((status <> 'pending') = (resolved_at IS NOT NULL)),
    CHECK ((status = 'accepted') = (resolved_memory_id IS NOT NULL))
);

CREATE INDEX idx_v2_memory_promotions_source
    ON v2_memory_promotions(source_memory_id, status);
