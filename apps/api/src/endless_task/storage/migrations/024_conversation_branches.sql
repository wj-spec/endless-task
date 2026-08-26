-- P4.5 会话分支与临时会话：谱系、身份与升级
ALTER TABLE conversations ADD COLUMN parent_conversation_id TEXT
    REFERENCES conversations(id) ON DELETE CASCADE;
ALTER TABLE conversations ADD COLUMN fork_turn_id TEXT;
ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'
    CHECK (kind IN ('normal', 'ephemeral'));
ALTER TABLE conversations ADD COLUMN promoted_at TEXT;

CREATE INDEX idx_conversations_parent ON conversations(parent_conversation_id);
