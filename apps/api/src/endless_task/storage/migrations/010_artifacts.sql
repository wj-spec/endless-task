CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('markdown', 'text')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    current_version_ordinal INTEGER NOT NULL DEFAULT 1
        CHECK (current_version_ordinal >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE TABLE artifact_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    content TEXT NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN ('create', 'update', 'chat_continue', 'rollback')),
    source_conversation_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    source_labels TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_labels)),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (artifact_id, ordinal)
);

CREATE INDEX idx_artifact_versions_artifact
    ON artifact_versions(artifact_id, ordinal);
