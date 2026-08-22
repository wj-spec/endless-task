CREATE TABLE uploaded_text_files (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX idx_uploaded_text_files_conversation_created
    ON uploaded_text_files(conversation_id, created_at, id);
