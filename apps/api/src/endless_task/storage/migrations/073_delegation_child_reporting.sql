-- 073_delegation_child_reporting.sql
-- 切片 9 M2：子运行结果"回流"到父会话所需的两枚标记。
--   * injected_at：该子运行最后一次被写进 <delegated-children> 注入块的时刻。
--     注入规则是"未终态 或者 (终态且 injected_at IS NULL)"——终态只注入一次，
--     否则每轮重复历史会把父上下文的 token 吃爆。
--   * reported_at：该子运行的结果已作为一条消息投递回父会话（唤醒链路）的时刻。
--     唤醒规则是"终态且 reported_at IS NULL"；投递成功时两枚标记一起置位
--     （会话正文已经拿到摘要，注入块不必再重复）。
--   * 部分索引只覆盖未汇报行：唤醒与注入查询都只看这一小撮。

ALTER TABLE delegation_child_runs ADD COLUMN injected_at TEXT;
ALTER TABLE delegation_child_runs ADD COLUMN reported_at TEXT;

CREATE INDEX IF NOT EXISTS idx_delegation_child_runs_unreported
    ON delegation_child_runs(parent_conversation_id)
    WHERE reported_at IS NULL;
