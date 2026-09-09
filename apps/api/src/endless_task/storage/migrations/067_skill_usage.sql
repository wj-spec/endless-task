-- 067_skill_usage.sql
-- S2 技能工作台：按 (scope, name, digest) 记录使用计数，便于"升级前后不混淆"。
--   * digest 参与主键：技能改内容后是新一行，旧数据保留可对比；
--   * kind: surfaced（进入模型目录）/ invoked（被显式调用）/ body_read（读正文）
--           / missing_dependencies（依赖不满足被隐藏）；
--   * last_at 供 UI 显示"最近使用"。

CREATE TABLE IF NOT EXISTS skill_usage (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('surfaced', 'invoked', 'body_read', 'missing_dependencies')
    ),
    count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    last_at TEXT,
    PRIMARY KEY (scope, name, digest, kind)
);

CREATE INDEX IF NOT EXISTS idx_skill_usage_name ON skill_usage(scope, name);
