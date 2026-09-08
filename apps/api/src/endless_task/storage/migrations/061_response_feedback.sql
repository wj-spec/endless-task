-- A3 反馈机制：用户对某条回答（response variant）的满意度反馈。
-- 幂等键 (turn_id, variant_id)；无 variant 时以 (turn_id) 唯一。
-- rating 作为偏好/RLHF 信号，供用户模型（B5）、记忆反思（B4）与评估（D1）复用。
CREATE TABLE response_feedback (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    variant_id TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    reason TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (turn_id, variant_id)
);

CREATE INDEX idx_response_feedback_turn ON response_feedback(turn_id);
CREATE INDEX idx_response_feedback_conversation ON response_feedback(conversation_id);
-- SQLite 的 UNIQUE(turn_id, variant_id) 对 NULL variant 不生效，补一个部分唯一索引。
CREATE UNIQUE INDEX idx_response_feedback_turn_no_variant
    ON response_feedback(turn_id)
    WHERE variant_id IS NULL;
