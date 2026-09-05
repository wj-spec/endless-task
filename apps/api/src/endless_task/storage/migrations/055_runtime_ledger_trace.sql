-- M6 OE-0: persistent trace/span/usage storage for the RuntimeLedger.
--
-- 08 4/5: the ledger is an append-only observability source. Spans carry
-- hierarchical trace correlation, events are the runtime journal, usage is
-- per-request canonical accounting. data/attributes columns store the
-- frozen JSON payloads produced by the ledger protocol (allowlisted
-- attributes only, so no secrets reach these tables by construction).
CREATE TABLE IF NOT EXISTS trace_spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    parent_span_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    monotonic_duration_ms REAL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    diagnostic_code TEXT,
    diagnostic_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_trace_spans_run ON trace_spans(run_id, started_at);
CREATE INDEX idx_trace_spans_trace ON trace_spans(trace_id);

CREATE TABLE IF NOT EXISTS trace_events (
    event_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    safety_critical INTEGER NOT NULL DEFAULT 0 CHECK (safety_critical IN (0, 1)),
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_trace_events_run ON trace_events(run_id, occurred_at);

CREATE TABLE IF NOT EXISTS trace_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_trace_usage_run ON trace_usage(run_id, occurred_at);
