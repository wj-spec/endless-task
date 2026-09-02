-- 051_runtime_v2_memory_update.sql
-- v2 记忆 UPDATE 语义:记忆可被更强/更新的同主题记忆取代。
-- 取代后旧记忆保留(superseded_by 指向新记忆,可追溯),查询层过滤。
-- 语义上等价于 v1 memories 的 superseded_by 机制。

ALTER TABLE v2_runtime_memories ADD COLUMN superseded_by TEXT;
ALTER TABLE v2_runtime_memories ADD COLUMN superseded_at TEXT;
