-- v1.1 会话模型：Lane lifecycle 与独立临时对话 provenance。
-- 兼容旧 v1.0 temporary/archived lane：保留 kind 取值，但新生命周期写入独立列。
ALTER TABLE v2_lanes ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'archived'));
ALTER TABLE v2_lanes ADD COLUMN archived_at TEXT;
ALTER TABLE v2_lanes ADD COLUMN display_name TEXT;
ALTER TABLE v2_lanes ADD COLUMN summary TEXT;
ALTER TABLE v2_lanes ADD COLUMN source_lane_id TEXT
    REFERENCES v2_lanes(id) ON DELETE SET NULL;
ALTER TABLE v2_lanes ADD COLUMN created_from_entry_id TEXT
    REFERENCES v2_transcript_entries(id) ON DELETE SET NULL;

-- 旧 archived kind 映射为 lifecycle 状态；kind 统一回落为持久分支，避免 UI/查询继续把 archived 当类型。
UPDATE v2_lanes
SET status = 'archived',
    archived_at = COALESCE(archived_at, created_at),
    kind = 'persistent_branch'
WHERE kind = 'archived';

UPDATE v2_lanes
SET created_from_entry_id = COALESCE(created_from_entry_id, base_entry_id)
WHERE base_entry_id IS NOT NULL;

CREATE INDEX idx_v2_lanes_conversation_status
    ON v2_lanes(conversation_id, status, created_at);
CREATE INDEX idx_v2_lanes_source_lane
    ON v2_lanes(source_lane_id);
CREATE INDEX idx_v2_lanes_created_from_entry
    ON v2_lanes(created_from_entry_id);

CREATE TABLE v2_temporary_conversations (
    conversation_id TEXT PRIMARY KEY,
    source_conversation_id TEXT,
    source_lane_id TEXT,
    source_base_entry_id TEXT,
    source_leaf_entry_id TEXT,
    snapshot_entry_ids_json TEXT NOT NULL DEFAULT '{"entryIds":[]}',
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (source_conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
    FOREIGN KEY (source_lane_id) REFERENCES v2_lanes(id) ON DELETE SET NULL,
    FOREIGN KEY (source_base_entry_id) REFERENCES v2_transcript_entries(id) ON DELETE SET NULL,
    FOREIGN KEY (source_leaf_entry_id) REFERENCES v2_transcript_entries(id) ON DELETE SET NULL
);

CREATE INDEX idx_v2_temporary_conversations_source
    ON v2_temporary_conversations(source_conversation_id, source_lane_id);