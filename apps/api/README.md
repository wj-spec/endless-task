# Endless Task API

`endless-task-api` 是单用户、本地优先的 Assistant Runtime 后端，当前包版本为 `0.5.0`，处于 Product Acceptance Candidate 阶段，尚未完成发布验收。

Python 负责模型适配、状态、审批与能力编排，Rust / PyO3 负责原生文件与检索内核。默认使用 `FakeProvider`，无需模型密钥即可启动；真实模型需单独配置。

## 安装与启动

macOS 优先。准备 Python 3.12、uv、rustup 和 Xcode Command Line Tools（`xcode-select --install`）。仓库通过 `rust-toolchain.toml` 固定 Rust `1.98.1`；首次安装需要下载相应工具链及依赖。Web 使用 Node.js 24。

在仓库根目录运行 `make install`，或仅安装后端：

```bash
cd apps/api
uv sync --locked --python 3.12 --extra dev --extra vec
.venv/bin/python ../../scripts/verify_native.py
.venv/bin/endless-task-api
```

源码安装通过 maturin 构建 PyO3 扩展。`dev` 包含后端测试依赖，`vec` 提供 SQLite 原生向量检索。安装后直接调用 `.venv/bin/` 中的命令，避免再次同步环境时丢失测试所需的 extra。

原生文件内核依赖 Unix 接口，当前验证以 macOS 为主；Linux 需单独核验，Windows 不在当前支持范围。

服务仅监听 `http://127.0.0.1:8000`；健康检查为 `GET /health`。前端启动见 [Web README](../web/README.md)。

## 数据与备份

macOS 默认数据库为 `~/Library/Application Support/Endless Task/endless-task.db`。Linux 使用 `$XDG_DATA_HOME/endless-task/endless-task.db`，未设置时回退到 `~/.local/share/endless-task/endless-task.db`。

可通过 `ENDLESS_TASK_DATA_DIR` 修改数据目录，或通过 `ENDLESS_TASK_DB_PATH` 指定数据库文件。从 `apps/api` 运行：

```bash
.venv/bin/endless-task data-path
.venv/bin/endless-task backup
# 也可指定尚不存在的备份文件
.venv/bin/endless-task backup /absolute/path/to/backup.db
```

默认备份写入数据库旁的 `backups/`，不会覆盖已有文件，并检查 SQLite 完整性。启动会应用随包附带的 schema 迁移；升级现有数据前先备份，评估新版本时宜使用独立数据目录。

恢复必须在 API、Worker 与 Scheduler 全部停止后执行：

```bash
ENDLESS_TASK_DB_PATH=/absolute/path/to/endless-task.db \
  .venv/bin/endless-task restore /absolute/path/to/backup.db --confirm
```

恢复会替换当前数据库，不合并备份之后的新数据；命令会为已有目标库创建安全备份。成功结果应包含 `integrity_check=ok` 和 `restored=true`。项目不提供 schema 降级，应用降级需恢复与旧版本兼容的备份。

## 模型配置

复制 [`.env.example`](.env.example) 为 `.env` 并填写自己的配置。服务读取当前目录的 `.env`，真实环境变量优先；也可通过 `ENDLESS_TASK_ENV_FILE` 指定文件。

```bash
cp .env.example .env
# 编辑 .env 后启动
.venv/bin/endless-task-api
```

DeepSeek 示例：

```text
ENDLESS_TASK_PROVIDER=deepseek
ENDLESS_TASK_BASE_URL=https://api.deepseek.com
ENDLESS_TASK_MODEL=deepseek-chat
DEEPSEEK_API_KEY=自己的密钥
```

OpenAI-compatible 示例：

```text
ENDLESS_TASK_PROVIDER=openai-compatible
ENDLESS_TASK_BASE_URL=https://example.com/v1
ENDLESS_TASK_MODEL=example-model
ENDLESS_TASK_API_KEY=自己的密钥
```

密钥只供后端使用，不能放入前端环境变量或提交仓库。配置真实模型后，模型请求会发送本轮上下文和工具读取的内容到所选服务。

## 能力与边界

- **Runtime v2**：Conversation / Lane、临时对话、持久分支、流式事件、幂等提交、检查点、取消与恢复。数据库约束限制每个 Lane 只有一个活动运行。
- **预算与续跑**：支持交互、任务与长跑预算档。只有预算硬停后 `completed`、最后的等待用户事件标记 `willStop=true`、工具结果均可确认、同一 Lane 没有其他活动运行时，才允许原地续跑同一条 run。普通完成、失败和取消不适用；崩溃恢复走独立路径。
- **文件与检索**：交互式 Agent 的 7 类文件工具接入 Rust 句柄路径；PyO3 扩展不可用时明确失败；安装和验证命令检查 ABI，拒绝不匹配的构建。工作区检索使用 Rust，当前仍为同步调用。网页保存、未值守执行等入口有各自实现，不能视为全部文件操作都已原生化。
- **记忆、成果与知识**：记忆提案及管理、成果版本与导出、知识源与混合检索。语义检索默认关闭，启用本地嵌入时首次需要下载模型。
- **任务与提醒**：提案确认、手工创建与编辑、调度、运行复核、通知、暂停 / 恢复 / 取消。
- **子运行委派**：后台运行、结果回传、并发与预算限制，支持只读与受配置控制的隔离写模式；隔离工作目录不等于完整 OS 隔离。
- **Skill / MCP**：Skill 管理、MCP 服务器配置与重载、工具白名单及审批。

只读工具可自动执行，绑定工作区内的文件写入按工具策略执行；删除、shell 等危险或外部动作需审批。审批只对相应调用与参数生效。shell 默认本机执行；Seatbelt / container 沙箱需显式配置并通过实际能力检查，不可用时拒绝启用。文件内核不能替代 OS 沙箱。

运行预算、工具超时、嵌入和沙箱选项见 [`.env.example`](.env.example)。本项目仅支持本机单用户使用，不能将 API 通过 `0.0.0.0`、端口转发或反向代理暴露到局域网 / 公网。

## 接口与源码入口

运行后通过 `http://127.0.0.1:8000/docs` 查看当前 OpenAPI；路由位于 `src/endless_task/api/routes/`。主要接口包括聊天与文件、Runtime v2 / SSE、分支、任务提醒、记忆成果、工作区、Provider、Skill 和 MCP。

| 关注点 | 入口 |
|---|---|
| 执行、恢复与预算 | `src/endless_task/runtime_v2/` |
| 子运行 | `src/endless_task/delegation/` |
| 文件 / shell 工具 | `src/endless_task/workspace_runtime/` |
| Rust 与 Python 绑定 | `crates/`、`src/endless_task/_native/` |
| 持久化与迁移 | `src/endless_task/storage/` |
| 离线评测 | `src/endless_task/eval/` |

设计要点见[根 README 的关键设计决策](../../README.md#关键设计决策)。

## 测试

仓库根目录的 `make test` 执行 Rust、原生扩展检查、后端测试和前端单测。仅运行后端相关检查时，从 `apps/api` 执行：

```bash
.venv/bin/python ../../scripts/verify_native.py
PYO3_PYTHON="$PWD/.venv/bin/python" cargo test --workspace --locked
TZ=Asia/Shanghai .venv/bin/python -m unittest discover -s tests
```

必须先确认原生扩展加载与 ABI 检查成功；不能把原生安全用例因缺少扩展而跳过当作验证通过。测试数量和跳过原因以实际输出为准，人工产品验收与发布验收另行进行。

## 离线评测

对已录制的 v2 Run 计算确定性指标，不调用模型或工具；`eval run` 会持久化评测批次。从 `apps/api` 运行：

```bash
.venv/bin/endless-task eval run --require-tools
.venv/bin/endless-task eval report --batch BATCH
.venv/bin/endless-task eval export --batch BATCH --format jsonl
.venv/bin/endless-task eval diff --baseline A --candidate B --tolerance approval_gate=0.05
.venv/bin/endless-task eval batches
```

指标包括 `completion`、`tool_correctness`、`approval_gate`、`robustness`、`efficiency` 和 `loop_detected`。`eval diff` 可用非零退出码阻止回退，当前仓库 CI 尚未自动接入该门禁。
