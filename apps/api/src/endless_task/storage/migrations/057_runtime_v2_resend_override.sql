-- P0-2b: 编辑消息重跑（resend）需在触发条目不变的情况下覆盖用户文案。
ALTER TABLE v2_runs ADD COLUMN trigger_content_override TEXT;
