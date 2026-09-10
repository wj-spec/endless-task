-- 070_skill_usage_search_hit.sql
-- S6：技能检索命中计数。
--   * SQLite 不能直接改 CHECK 约束，只能重建表；
--   * 新增 kind: search_hit（skill_search 命中该技能）；
--   * 同时保留既有计数与索引。

CREATE TABLE skill_usage_new (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('surfaced', 'invoked', 'body_read', 'missing_dependencies', 'search_hit')
    ),
    count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    last_at TEXT,
    PRIMARY KEY (scope, name, digest, kind)
);

INSERT INTO skill_usage_new(scope, name, digest, kind, count, last_at)
SELECT scope, name, digest, kind, count, last_at FROM skill_usage;

DROP TABLE skill_usage;

ALTER TABLE skill_usage_new RENAME TO skill_usage;

CREATE INDEX IF NOT EXISTS idx_skill_usage_name ON skill_usage(scope, name);
