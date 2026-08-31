-- Evaluation layer: persisted offline eval batches and per-run results.
-- The full RunEvaluation is stored in `result_json`; `run_id` is denormalized
-- for batch-level queries. A normalized `eval_scores` table is deferred to a
-- later phase (metric-level querying/graphing).
CREATE TABLE IF NOT EXISTS eval_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    judge_provider TEXT,
    judge_model TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    aggregate_json TEXT
);

CREATE TABLE IF NOT EXISTS eval_run_results (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES eval_batches(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_eval_run_results_batch ON eval_run_results(batch_id, run_id);
