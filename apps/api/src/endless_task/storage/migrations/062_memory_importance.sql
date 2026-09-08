-- 062_memory_importance.sql
-- B3 重要性加权遗忘 / 间隔重复：记忆带上重要性、访问次数与钉住标记。
-- 遗忘概率 p ∝ exp(-importance) 且随访问次数下降（间隔重复），pinned 永不自动遗忘。
-- 旧数据按默认重要性 0.5、零访问、未钉住处理，行为与迁移前一致。

ALTER TABLE memories ADD COLUMN importance REAL NOT NULL DEFAULT 0.5;
ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_accessed_at TEXT;
ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
