-- 064_undo_journal.sql
-- A5 撤销/回滚：记录可逆的工具副作用及其"变更前状态"，供用户一键撤销。
--   before_content: 变更前的文件内容；before_exists=0 表示当时文件不存在
--                   （撤销 = 删除该文件）。
--   status: available（可撤销）/ undone（已撤销，重复撤销幂等返回）。
-- 只记录**可逆**操作（文件写/删）；不可逆操作（外部动作、命令副作用）不入表，
-- 前端据此不提供撤销入口（与 A1 审批一致：撤销本身也是一次写操作）。

CREATE TABLE undo_journal (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    workspace_id TEXT,
    run_id TEXT,
    tool_execution_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('file_write', 'file_delete')),
    target TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    before_exists INTEGER NOT NULL CHECK (before_exists IN (0, 1)),
    before_content TEXT,
    after_hash TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available', 'undone')),
    created_at TEXT NOT NULL,
    undone_at TEXT
);

CREATE INDEX idx_undo_journal_conversation
    ON undo_journal(conversation_id, created_at DESC);
