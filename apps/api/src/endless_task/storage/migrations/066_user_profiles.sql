-- 066_user_profiles.sql
-- B5 用户画像：跨会话注入的"稳定前缀块"。
-- 设计要点（缓存/成本纪律）：
--   * 一行 = 一份画像（按 scope_key 唯一，通常是 workspace id 或 'general'）；
--   * content 是**确定性渲染**后的文本（排序固定、无时间戳），同一版本字节一致；
--   * version 只在签名变化时 +1，未变化时不写库，从而不破坏 provider 前缀缓存；
--   * manual=1 表示用户手写覆盖，自动重建不覆盖它。

CREATE TABLE user_profiles (
    scope_key TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    manual INTEGER NOT NULL DEFAULT 0 CHECK (manual IN (0, 1)),
    source_memory_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
