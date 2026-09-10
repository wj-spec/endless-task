-- 068_notification_turn.sql
-- 通知定位：通知需要能直接把用户带到"触发它的那一轮"。
--   * task_runs.turn_id 存的是 v2 run id（worker 在提交后 link_turn 写入）；
--   * 通知原来只有 task run id，前端无法据此定位到具体轮次；
--   * 这里给 notifications 补一列 turn_id（v2 run id），旧行保持 NULL。

ALTER TABLE notifications ADD COLUMN turn_id TEXT;
