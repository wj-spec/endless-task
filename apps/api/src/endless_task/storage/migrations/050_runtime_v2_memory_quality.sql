-- 050_runtime_v2_memory_quality.sql
-- v2 记忆质量支持:加 expired_at 列(过期过滤),供时效记忆与记忆质量闭环使用。
-- 过期判定在查询层(expired_at IS NULL OR expired_at > now),并提供生命周期方法
-- 把已过期记忆软删(status='deleted'),无需改动 status CHECK 约束。

ALTER TABLE v2_runtime_memories ADD COLUMN expired_at TEXT;
