ALTER TABLE runtime_events ADD COLUMN response_variant_id TEXT;
ALTER TABLE runtime_events ADD COLUMN message_id TEXT;

CREATE INDEX idx_runtime_events_variant_sequence
    ON runtime_events(response_variant_id, sequence);

CREATE UNIQUE INDEX idx_runtime_events_singleton_per_variant
    ON runtime_events(response_variant_id, event_type)
    WHERE event_type IN (
        'turn.started',
        'message.started',
        'message.completed',
        'turn.completed',
        'turn.failed',
        'turn.cancelled'
    );
