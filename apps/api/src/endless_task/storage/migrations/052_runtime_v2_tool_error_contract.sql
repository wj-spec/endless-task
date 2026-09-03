-- Preserve normalized tool failure metadata across restart, replay, and UI snapshots.

ALTER TABLE v2_tool_executions
    ADD COLUMN retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1));
ALTER TABLE v2_tool_executions ADD COLUMN error_correlation_id TEXT;
ALTER TABLE v2_tool_executions ADD COLUMN error_details_json TEXT;