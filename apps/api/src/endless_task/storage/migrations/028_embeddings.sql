CREATE TABLE IF NOT EXISTS embeddings (
    scope TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, ref_id, model)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model_scope
    ON embeddings (model, scope);
