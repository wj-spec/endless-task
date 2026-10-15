-- 074_internal_execution_conversations.sql
-- 批次 B Task 1（F1 修复）：定时任务 / 提醒的到点执行落进**自己的正式执行会话**。
--
-- 背景：到点执行此前调 v1
-- `create_branch`，后者在 `fork_turn_id is None` 时必须读到父会话的 v1 `turns` 行；
-- 而生产代码里 v1 `turns` 零写入者 → 必抛 InvalidStateError → 被吞成 None →
-- `【到点执行】…` 落进**用户的日常会话**。ADR-1 裁定改用的对象是"任务 / 提醒
-- **自己的正式会话**"（`kind='normal'` + 内部标记），而不是父会话下的
-- `kind='ephemeral'` 子会话（那正是 D-OQ-6 要消除的形态）。
--
--   * `internal_owner_kind`：谁拥有这份执行记录——`'task_execution'` /
--     `'reminder_execution'`。
--   * `internal_owner_id`：task id / reminder id，与 kind 一起构成"一个任务一份
--     执行记录"的复用键（找到就复用，找不到才新建，避免每次到点涨库 + 左栏风暴）。
--
-- 取舍（照 `054_delegation_conversation_parent.sql` 的形状）：
--   * **加 CHECK 是免费的**：本仓库 `024_conversation_branches.sql:5-6` 已经证明
--     `ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'
--     CHECK (kind IN ('normal','ephemeral'))` 是**纯 ADD COLUMN、零重建表**，所以这里
--     照先例把取值钉死在库里（`internal_owner_kind` 可空——NULL 表"不是内部执行会话"；
--     非 NULL 时只认这两个值）。仓储层（`create_internal_conversation` 的入参）仍然
--     收口取值，但那是**双保险**，不是"因为加不了 CHECK 才只能靠仓储层"。
--   * **不加 FOREIGN KEY**：与 054 同理——执行会话在 `link_conversation` 之前就建出来，
--     而标记写的是 task/reminder id，生命周期由 worker 的查找/创建路径收口。
--   * 两列都可空：存量会话与委派子会话一律 NULL，迁移后行为不变。
--   * 局部索引只覆盖带标记的行（执行会话是一小撮），空表时索引也为空。
--     注意：**唯一性**由 `075` 的局部唯一索引收口（同一 owner 在 active 里只有一份），
--     本文件的这个索引只服务"按 owner 查"。

ALTER TABLE conversations ADD COLUMN internal_owner_kind TEXT
    CHECK (internal_owner_kind IN ('task_execution', 'reminder_execution'));
ALTER TABLE conversations ADD COLUMN internal_owner_id TEXT;

CREATE INDEX idx_conversations_internal_owner
    ON conversations(internal_owner_kind, internal_owner_id)
    WHERE internal_owner_id IS NOT NULL;
