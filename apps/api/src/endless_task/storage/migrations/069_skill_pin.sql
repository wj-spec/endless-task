-- 069_skill_pin.sql
-- S5 技能精选：用户可以把技能"固定"进默认目录。
--   * 与 disabled 共用 skill_overrides，避免两套覆盖表；
--   * 默认 0：未固定的技能按"工作区 > 最近常用"参与精选与折叠。

ALTER TABLE skill_overrides ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
