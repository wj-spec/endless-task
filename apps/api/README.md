# Endless Task API

P0 本地 Assistant Runtime 与 HTTP API 的 Python 后端包。

P0 已完成并通过 R0.8 发布验收：

- P0 Chat 领域模型。
- SQLite 初始 Schema。
- 版本化迁移。
- ChatRepository 协议。
- SQLite Repository。
- 持久化、幂等和 ResponseVariant 测试。
- AssistantRuntime 与 TurnController。
- FakeProvider 与 Provider-neutral 事件。
- CancellationManager。
- SQLite RuntimeEventJournal 与事件重放。
- FastAPI Conversation/Turn API。
- SSE 实时事件、持久化重放、heartbeat 与 `Last-Event-ID`。
- 统一 HTTP 错误结构。
- OpenAI-compatible Chat Completions Provider。
- DeepSeek、OpenAI 和自定义兼容服务配置。
- Provider 超时、取消、错误与 token usage 归一化。
- 有预算上限的 canonical conversation context。
- 完整历史 Turn 的成对裁剪，排除失败与取消回答。
- 本地派生的有界会话摘要及版本记录。
- 每个 ResponseVariant 的 ContextSnapshot 审计记录。
- 固定回环地址与 Trusted Host 防护。
- 日志密钥脱敏和模型并发上限。
- 稳定的用户数据目录、在线 SQLite 备份及完整性检查。

默认仍使用 FakeProvider，未配置密钥时不会访问外部服务。

## 本地启动

```bash
cd apps/api
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/endless-task-api
```

服务只监听 `http://127.0.0.1:8000`。macOS 默认数据文件位于：

```text
~/Library/Application Support/Endless Task/endless-task.db
```

Linux 默认使用 `$XDG_DATA_HOME/endless-task/endless-task.db`，未配置时回退到
`~/.local/share/endless-task/endless-task.db`。可通过 `ENDLESS_TASK_DATA_DIR` 修改数据目录，或用 `ENDLESS_TASK_DB_PATH` 精确指定数据库文件。

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

## 使用 DeepSeek

复制示例配置并填写自己的 Key；`.env` 已被 Git 忽略：

```bash
cd apps/api
cp .env.example .env
# 编辑 .env 中的 DEEPSEEK_API_KEY
set -a
source .env
set +a
uv run endless-task-api
```

DeepSeek 默认配置为：

```text
ENDLESS_TASK_PROVIDER=deepseek
ENDLESS_TASK_BASE_URL=https://api.deepseek.com
ENDLESS_TASK_MODEL=deepseek-chat
```

也可以使用 `ENDLESS_TASK_API_KEY` 作为通用密钥变量。密钥只从进程环境读取，不会进入浏览器、SQLite、API 响应或应用日志。

其他 OpenAI-compatible 服务使用：

```text
ENDLESS_TASK_PROVIDER=openai-compatible
ENDLESS_TASK_BASE_URL=https://example.com/v1
ENDLESS_TASK_MODEL=example-model
ENDLESS_TASK_API_KEY=...
```

## 上下文预算

默认设置适合通用的 OpenAI-compatible 模型，并可通过环境变量覆盖：

```text
ENDLESS_TASK_CONTEXT_WINDOW_TOKENS=32768
ENDLESS_TASK_MAX_OUTPUT_TOKENS=2048
ENDLESS_TASK_SUMMARY_TOKEN_LIMIT=1024
ENDLESS_TASK_SYSTEM_PROMPT_VERSION=p0-v1
ENDLESS_TASK_MAX_CONCURRENT_MODEL_CALLS=2
ENDLESS_TASK_MAX_MESSAGE_CHARACTERS=100000
```

Runtime 会先为输出预留预算，再保留系统提示和当前消息，最后纳入本地摘要与最近的完整 Turn。当前消息不会被静默截断；无法安全放入上下文时返回 `context_too_large`。

## 运行测试

```bash
cd apps/api
.venv/bin/python -W error -m unittest discover -s tests -v
```

在受限 macOS 环境中，可以额外设置 `PYTHONPYCACHEPREFIX` 指向临时目录。
