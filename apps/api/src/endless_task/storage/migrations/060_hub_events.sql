-- P1-2 hub 全局事件面：跨会话通知/提案变更事件（独立于 conversation-scoped
-- v2_product_events；hub 角标是 App 顶层全局消费，无单一 conversation 上下文）。
-- event_seq 为全库单调递增游标；SSE 端点按 after_seq 增量订阅。
CREATE TABLE hub_events (
    id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    conversation_id TEXT,
    data TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (event_seq)
);

CREATE INDEX idx_hub_events_sequence ON hub_events(event_seq);
CREATE INDEX idx_hub_events_conversation ON hub_events(conversation_id);
