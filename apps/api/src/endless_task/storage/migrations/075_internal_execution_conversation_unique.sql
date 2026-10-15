-- 075_internal_execution_conversation_unique.sql
-- 批次 B Task 1 复审 M-2：「一个任务一份执行记录」从"靠 `ORDER BY updated_at DESC
-- LIMIT 1` 隐式兜底"升级为**库级不变量**。
--
-- 背景：074 只建了普通索引 `idx_conversations_internal_owner`，`(owner_kind,
-- owner_id)` 上没有唯一性。`find_internal_conversation` 只认 `status='active'`，
-- 所以同一 owner 在 active 里出现两行时，"哪一份"由 `updated_at DESC, id DESC`
-- 隐式决定（时间戳相同时连排序都不稳定）。今天唯一的写入者是
-- `create_internal_conversation` 的"先查后建"，单线程下不会造出重复，但没有任何
-- 东西在库一级挡住它——并发到点或未来的第二条写入路径都能造出两份。
--
-- 做法照本仓库既有先例 `053_task_run_atomic_claim.sql:26-28` 的**部分唯一索引**
-- （`CREATE UNIQUE INDEX … WHERE status='running'`）：只约束"active 的执行记录"，
-- 「用户删除执行会话 → 下次到点新建一份」的既有语义不受影响（被删的行不再是 active；
-- 归档的行也不再是 active，所以"归档 + 新建"是合法的两行）。
--
-- 先归一再建索引（同样照 053 的收尾）：生产库不可能有重复（074 之后才有写入者，
-- 且只有一条写入路径），但迁移对**任何**库都要幂等成立——若真有同 owner 多份 active
-- 执行记录，保留最近更新的那一份（与 `find_internal_conversation` 原来的选择一致），
-- 其余归档，理由写进 `archived_at` 之外不改动标题/内容（归档不销毁数据）。

UPDATE conversations
SET status = 'archived',
    archived_at = COALESCE(
        archived_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'active'
  AND internal_owner_id IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM conversations AS survivor
      WHERE survivor.internal_owner_kind = conversations.internal_owner_kind
        AND survivor.internal_owner_id = conversations.internal_owner_id
        AND survivor.status = 'active'
        AND (
            survivor.updated_at > conversations.updated_at
            OR (
                survivor.updated_at = conversations.updated_at
                AND survivor.id > conversations.id
            )
        )
  );

CREATE UNIQUE INDEX idx_conversations_internal_owner_active
    ON conversations(internal_owner_kind, internal_owner_id)
    WHERE internal_owner_id IS NOT NULL AND status = 'active';
