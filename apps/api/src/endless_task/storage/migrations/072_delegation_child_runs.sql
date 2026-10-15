-- 072_delegation_child_runs.sql
-- 委派子运行记录（切片 9）：spawn 之后子运行在后台独立推进，父运行那一轮早已结束，
-- 所以"子运行现在怎么样"必须能跨轮次、跨进程重启回答。
--   * child_run_id 由 (parent_run_id, parent_tool_call_id) 确定性派生
--     （delegation/coordinator.py 的 _child_run_id_for），因此
--     UNIQUE(parent_run_id, parent_tool_call_id) 直接取代了内存里的 _spawn_keys：
--     进程重启后重复派发同一个父工具调用会撞唯一约束，仍然被拒。
--   * state: running（在跑）/ completed / failed / cancelled /
--     interrupted（进程死在半路，由启动对账写入）。
--   * summary 是终态结果的有界投影（delegation/result_projection.py），不是子运行全文。
--   * 有意不记子运行自己的 conversation id：它由 runtime_v2 适配器创建，协调器看不到。

CREATE TABLE IF NOT EXISTS delegation_child_runs (
    child_run_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL,
    parent_tool_call_id TEXT NOT NULL,
    parent_conversation_id TEXT NOT NULL DEFAULT '',
    child_workspace_id TEXT,
    depth INTEGER NOT NULL,
    capability_profile TEXT NOT NULL,
    workspace_mode TEXT NOT NULL,
    timeout_seconds REAL NOT NULL,
    task TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')
    ),
    summary TEXT,
    error_code TEXT,
    safe_message TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    model_turns INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    terminal_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (parent_run_id, parent_tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_delegation_child_runs_parent
    ON delegation_child_runs(parent_run_id);

CREATE INDEX IF NOT EXISTS idx_delegation_child_runs_state
    ON delegation_child_runs(state);
