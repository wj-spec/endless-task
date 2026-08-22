CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    next_turn_ordinal INTEGER NOT NULL DEFAULT 1 CHECK (next_turn_ordinal >= 1),
    title_is_manual INTEGER NOT NULL DEFAULT 0 CHECK (title_is_manual IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CHECK (
        (status = 'active' AND archived_at IS NULL)
        OR (status = 'archived' AND archived_at IS NOT NULL)
    )
);

CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    user_message_id TEXT NOT NULL,
    active_response_variant_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('created', 'running', 'completed', 'failed', 'cancelled')
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_message_id) REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (active_response_variant_id) REFERENCES response_variants(id)
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (conversation_id, ordinal)
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE TABLE response_variants (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL UNIQUE,
    variant_index INTEGER NOT NULL CHECK (variant_index >= 1),
    operation TEXT NOT NULL CHECK (operation IN ('create', 'retry', 'regenerate')),
    status TEXT NOT NULL CHECK (
        status IN ('created', 'running', 'completed', 'failed', 'cancelled')
    ),
    provider TEXT,
    model TEXT,
    finish_reason TEXT CHECK (
        finish_reason IS NULL
        OR finish_reason IN ('stop', 'length', 'content_filter', 'error', 'cancelled')
    ),
    error_code TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE CASCADE,
    UNIQUE (turn_id, variant_index)
);

CREATE UNIQUE INDEX idx_messages_one_user_per_turn
    ON messages(turn_id)
    WHERE role = 'user';

CREATE UNIQUE INDEX idx_turns_one_active_per_conversation
    ON turns(conversation_id)
    WHERE status IN ('created', 'running');

CREATE UNIQUE INDEX idx_variants_one_active_per_turn
    ON response_variants(turn_id)
    WHERE status IN ('created', 'running');

CREATE INDEX idx_conversations_status_updated
    ON conversations(status, updated_at DESC);

CREATE INDEX idx_turns_conversation_ordinal
    ON turns(conversation_id, ordinal);

CREATE INDEX idx_messages_turn
    ON messages(turn_id);

CREATE TABLE client_requests (
    conversation_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    turn_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, client_request_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE TABLE command_requests (
    turn_id TEXT NOT NULL,
    command_request_id TEXT NOT NULL,
    command_type TEXT NOT NULL CHECK (command_type IN ('retry', 'regenerate')),
    response_variant_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (turn_id, command_request_id),
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    FOREIGN KEY (response_variant_id) REFERENCES response_variants(id) ON DELETE CASCADE
);

CREATE TABLE context_snapshots (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    response_variant_id TEXT NOT NULL UNIQUE,
    system_prompt_version TEXT NOT NULL,
    summary_revision_id TEXT,
    included_turns_json TEXT NOT NULL,
    input_token_estimate INTEGER NOT NULL CHECK (input_token_estimate >= 0),
    reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    FOREIGN KEY (response_variant_id) REFERENCES response_variants(id) ON DELETE CASCADE
);

CREATE TABLE runtime_events (
    event_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    UNIQUE (turn_id, sequence)
);

CREATE INDEX idx_runtime_events_turn_sequence
    ON runtime_events(turn_id, sequence);
