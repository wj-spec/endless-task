-- 049_runtime_v2_migrate_v1_memories.sql
-- 把 v1 memories(active)迁移为 v2 user_global 记忆,统一 v2 时代的记忆读取来源。
-- 迁移后:v2 会话的记忆全部来自 v2_runtime_memories(含本次迁移的存量 + 新写入),
-- v1 表仅作为 v1 只读运行时的来源,不再被 v2 链路读取(消除双注入)。
-- 幂等由 schema_migrations 保证(该迁移只执行一次)。
-- 只迁移 source conversation 仍存在的记忆(避免外键失败);已删除会话的记忆丢弃。

INSERT INTO v2_runtime_memories (
    id, scope, kind, content, status, conversation_id,
    workspace_id, lane_id, run_id, source_memory_id, source_entry_id,
    created_at, updated_at
)
SELECT
    'v2mem_mig_' || m.id,
    'user_global',
    m.kind,
    m.content,
    'active',
    m.source_conversation_id,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    m.created_at,
    m.updated_at
FROM memories AS m
WHERE m.status = 'active'
  AND EXISTS (
      SELECT 1 FROM conversations AS c WHERE c.id = m.source_conversation_id
  );
