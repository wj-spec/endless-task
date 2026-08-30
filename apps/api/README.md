# Endless Task API

`endless-task-api` 是 Endless Task 的本地后端包，当前包版本为 `0.5.0`，处于 Runtime v2 v1.1 Feature Integration Candidate 阶段，尚不是 Release Candidate。

它不是通用 Agent 平台，而是一个单用户、本地优先的 Assistant Runtime：普通聊天走 Assistant Runtime；需要工具时进入受限 Agent Loop；记忆、成果和长期任务作为能力层被 Assistant 按需调用。

## Runtime 范围

- `Runtime v2`：Conversation 单活动 Run、持久化 Lane/Branch、Temporary Conversation、全局事件序号、snapshot/SSE 恢复和消息幂等。
- `Assistant Runtime`：Conversation、Turn、Message、ResponseVariant、上下文构建、SSE 流式事件、停止/重试/重新生成。
- `Agent Runtime`：Provider-neutral Tool Calling、工具参数校验、循环/调用/超时限制、取消传播、只读工具自动执行、副作用审批。
- `Memory Runtime`：记忆提案、用户确认写入、跨会话注入、查看/编辑/删除、来源/冲突/过期处理。
- `Artifact Runtime`：Artifact 提案、版本管理、来源引用、聊天继续修改、Markdown/HTML/PDF 导出、按需工作区。
- `Task Runtime`：任务提案、用户确认、Worker、Scheduler、暂停/恢复/取消、运行复核、通知、幂等、失败恢复、错过调度补偿和一次性提醒。

默认 Provider 是 `FakeProvider`，未配置密钥时不会访问外部服务。当前默认工具仍以只读能力为主，不把真实写操作或外部副作用工具作为已开放能力展示。

## 主要接口面

```text
GET  /health

POST /conversations
GET  /conversations
GET  /conversations/{id}
PATCH /conversations/{id}
DELETE /conversations/{id}
POST /conversations/{id}/turns
GET  /turns/{id}
GET  /turns/{id}/events
POST /turns/{id}/cancel
POST /turns/{id}/retry
POST /turns/{id}/regenerate
POST /turns/{id}/response-variants/{variantId}/select

POST   /conversations/{conversationId}/files?filename=notes.md
DELETE /conversations/{conversationId}/files/{fileId}
POST   /approvals/{approvalId}

GET  /conversations/{conversationId}/memory-proposals
POST /memory-proposals/{proposalId}/resolve
GET  /memories
PATCH /memories/{memoryId}
DELETE /memories/{memoryId}

GET  /conversations/{conversationId}/artifact-proposals
POST /artifact-proposals/{proposalId}/resolve
GET  /conversations/{conversationId}/workspace
GET  /artifacts/{artifactId}
GET  /artifacts/{artifactId}/versions
POST /artifacts/{artifactId}/rollback
GET  /artifacts/{artifactId}/export

GET  /conversations/{conversationId}/task-proposals
POST /task-proposals/{proposalId}/resolve
GET  /tasks
GET  /tasks/{taskId}/runs
POST /tasks/{taskId}/run
POST /tasks/{taskId}/pause
POST /tasks/{taskId}/resume
POST /tasks/{taskId}/cancel
GET  /reminders
POST /reminders/{reminderId}/cancel
GET  /notifications
POST /notifications/{notificationId}/read
POST /notifications/read-all
GET  /proposals/pending

GET  /settings/permissions
POST /settings/permissions
```

需要副作用的工具会产生 `approval.requested` SSE 事件，并暂停当前 Turn。批准只对本次工具调用生效；参数变化、服务重启、超时或取消都会使原审批失效。

## 本地启动

```bash
cd apps/api
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
uv run endless-task-api
```

服务只监听 `http://127.0.0.1:8000`。macOS 默认数据文件位于：

```text
~/Library/Application Support/Endless Task/endless-task.db
```

Linux 默认使用 `$XDG_DATA_HOME/endless-task/endless-task.db`，未配置时回退到：

```text
~/.local/share/endless-task/endless-task.db
```

可通过 `ENDLESS_TASK_DATA_DIR` 修改数据目录，或用 `ENDLESS_TASK_DB_PATH` 精确指定数据库文件。

查看实际数据位置：

```bash
uv run endless-task data-path
```

创建在线一致性备份：

```bash
uv run endless-task backup
# 或指定一个尚不存在的目标文件
uv run endless-task backup /path/to/endless-task-backup.db
```

默认备份写入数据库同级的 `backups/` 目录。命令不会覆盖已有文件，并会在发布备份前运行 SQLite 完整性检查。

## Runtime v2 升级与恢复

以下命令只适用于应用已经停止、没有 API/Worker/Scheduler 继续写数据库的维护窗口。建议显式设置数据库路径，避免操作到错误环境：

```bash
cd apps/api
export ENDLESS_TASK_DB_PATH="/absolute/path/to/endless-task.db"
uv run endless-task data-path
```

升级顺序：

```bash
# 1. 在一致性临时副本上预演，不修改源库
uv run endless-task migrate-runtime-v2 --dry-run

# 2. 先创建升级前备份，再应用 schema 和 Runtime v2 数据迁移
uv run endless-task migrate-runtime-v2 --apply

# 3. 在临时副本上只读审计当前库
uv run endless-task migrate-runtime-v2 --audit

# 4. 可重复执行；已完成时应返回 already_migrated=true
uv run endless-task migrate-runtime-v2 --apply
```

判定规则：

- 当前 Runtime v2 schema 最大迁移为 `045_runtime_v2_promoted_main_lane_repair.sql`；它会修复已记录旧版 `044`、但尚未完成主线 Lane 角色归一化的候选数据库。
- dry-run 必须返回 `source_unchanged=true`。
- apply 必须返回 `backup=<path>` 和 `migrated=true`；失败时返回 `migrated=false`、退出码 1，并保留已打印的升级前备份路径。
- audit 通过时返回 `passed=true`、`pending_migration_conversations=0` 和 `rollback_reconciliation_trees=0`。audit 使用一致性临时副本；即使源库 schema 较旧，也不会先升级源库。
- 任一步失败都不要启动应用或让旧应用直接连接已升级数据库；先保留命令输出和备份。

恢复顺序：

```bash
# 应用必须保持停止；命令会先为当前目标库创建 safety_backup
uv run endless-task restore /absolute/path/to/pre-upgrade-backup.db --confirm
```

恢复成功必须出现 `integrity_check=ok` 和 `restored=true`。恢复会替换当前数据库，不合并升级后新增数据；`safety_backup=<path>` 保存替换前状态。项目不提供 down migration，Runtime 切换也不会回滚 schema；如需应用版本降级，必须恢复与目标旧版本兼容的升级前备份后再启动该版本。

## Provider 配置

复制示例配置并填写自己的 Key；`.env` 已被 Git 忽略。服务启动时会自动读取当前目录的 `.env`（真实环境变量优先），也可用 `ENDLESS_TASK_ENV_FILE` 指定配置文件路径：

```bash
cd apps/api
cp .env.example .env
# 编辑 .env 中的 DEEPSEEK_API_KEY 或 ENDLESS_TASK_API_KEY
uv run endless-task-api
```

DeepSeek 默认配置：

```text
ENDLESS_TASK_PROVIDER=deepseek
ENDLESS_TASK_BASE_URL=https://api.deepseek.com
ENDLESS_TASK_MODEL=deepseek-chat
```

OpenAI-compatible 服务配置：

```text
ENDLESS_TASK_PROVIDER=openai-compatible
ENDLESS_TASK_BASE_URL=https://example.com/v1
ENDLESS_TASK_MODEL=example-model
ENDLESS_TASK_API_KEY=...
```

密钥只从进程环境读取，不进入浏览器、SQLite、API 响应或应用日志。

## 关键运行参数

```text
ENDLESS_TASK_CONTEXT_WINDOW_TOKENS=32768
ENDLESS_TASK_MAX_OUTPUT_TOKENS=2048
ENDLESS_TASK_SUMMARY_TOKEN_LIMIT=1024
ENDLESS_TASK_SYSTEM_PROMPT_VERSION=p1-v1
ENDLESS_TASK_MAX_CONCURRENT_MODEL_CALLS=2
ENDLESS_TASK_MAX_TASK_RUNS=1
ENDLESS_TASK_MAX_MESSAGE_CHARACTERS=100000
ENDLESS_TASK_MAX_AGENT_ITERATIONS=4
ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN=8
ENDLESS_TASK_AGENT_TIMEOUT_SECONDS=120
ENDLESS_TASK_APPROVAL_TIMEOUT_SECONDS=1800
ENDLESS_TASK_MAX_FILE_BYTES=1000000
ENDLESS_TASK_MAX_FILES_PER_CONVERSATION=10
ENDLESS_TASK_MEMORY_PROPOSALS=1
ENDLESS_TASK_ARTIFACT_PROPOSALS=1
ENDLESS_TASK_TASK_PROPOSALS=1
ENDLESS_TASK_SCHEDULER=1
ENDLESS_TASK_SCHEDULER_TICK=30
ENDLESS_TASK_RUN_REVIEW=1
ENDLESS_TASK_NOTIFICATIONS=1
ENDLESS_TASK_TASK_MAX_ATTEMPTS=3
ENDLESS_TASK_TASK_RETRY_BACKOFF=60
```

Runtime 会先为输出预留预算，再保留系统提示和当前消息，最后纳入本地摘要、记忆和最近的完整 Turn。当前消息不会被静默截断；无法安全放入上下文时返回 `context_too_large`。

## 运行测试

当前开发基线：`uv run python -W error -m unittest discover -s tests -v`，616/616 通过。测试日志仍可能出现临时数据库关闭后的已知异步清理错误；最终 unittest 结果不受影响，但该生命周期问题仍是发布前治理项。

```bash
cd apps/api
uv run python -W error -m unittest discover -s tests -v
```

在受限 macOS 环境中，可以额外设置 `PYTHONPYCACHEPREFIX` 指向临时目录。

重点测试文件：

- `tests/test_p0_release_gate.py`
- `tests/test_p1_release_gate.py`
- `tests/test_memory_*.py`
- `tests/test_artifact_*.py`
- `tests/test_task_*.py`
