-- R5.7 检索埋点与知识文件溯源
CREATE TABLE retrieval_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('injection', 'search', 'citation_click')),
    query TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    turn_id TEXT,
    hit_counts TEXT NOT NULL DEFAULT '{}',
    zero_hit INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_retrieval_events_created ON retrieval_events(created_at);
CREATE INDEX idx_retrieval_events_turn ON retrieval_events(turn_id);

ALTER TABLE knowledge_sources ADD COLUMN file_size INTEGER;
ALTER TABLE knowledge_sources ADD COLUMN file_sha256 TEXT;
