-- R6.3 多 Provider：全局 profile、默认选择与会话级覆盖。
CREATE TABLE IF NOT EXISTS provider_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url TEXT NOT NULL DEFAULT '',
    api_key_ref TEXT NOT NULL DEFAULT '',
    default_model TEXT NOT NULL,
    timeout_seconds REAL NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE conversations ADD COLUMN provider_profile_id TEXT
    REFERENCES provider_profiles(id) ON DELETE SET NULL;
ALTER TABLE conversations ADD COLUMN model_override TEXT;

ALTER TABLE preferences ADD COLUMN default_provider_profile_id TEXT
    REFERENCES provider_profiles(id) ON DELETE SET NULL;

CREATE INDEX idx_conversations_provider
    ON conversations(provider_profile_id);
