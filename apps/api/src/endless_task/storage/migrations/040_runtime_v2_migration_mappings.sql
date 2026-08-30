CREATE TABLE v2_migration_conversation_mappings (
    source_conversation_id TEXT PRIMARY KEY,
    tree_conversation_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    source_parent_conversation_id TEXT,
    source_fork_turn_id TEXT,
    lane_kind TEXT NOT NULL,
    v1_read_only INTEGER NOT NULL DEFAULT 1 CHECK (v1_read_only IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (tree_conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (lane_id) REFERENCES v2_lanes(id) ON DELETE CASCADE,
    FOREIGN KEY (source_parent_conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);

CREATE INDEX idx_v2_migration_conversation_mappings_tree
    ON v2_migration_conversation_mappings(tree_conversation_id);
