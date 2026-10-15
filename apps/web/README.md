# Endless Task Web

`@endless-task/web` 是 Endless Task 的本地聊天前端，当前包版本为 `0.5.0`，处于 Product Acceptance Candidate 阶段，尚未完成发布验收。

聊天是主要入口，文件、会话、成果、变更和终端通过辅助面板呈现；记忆、任务、提醒和通知结合对话使用。

## 安装与启动

需要 Node.js 24。先按 [API README](../api/README.md) 安装并启动后端，或在仓库根目录运行 `make install`、`make api`，再另开终端运行 `make web`。

仅安装和启动前端时：

```bash
cd apps/web
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite 把 API 与终端 WebSocket 请求代理到 `http://127.0.0.1:8000`；可通过 `ENDLESS_TASK_API_URL` 指定本地后端地址。

后端默认使用 FakeProvider。真实模型只在后端配置，模型密钥不能进入前端环境变量。

## 检查

从 `apps/web` 运行：

```bash
npm run build
npm run test:unit
# 首次运行 E2E 需安装浏览器
npx playwright install chromium
npm run test:e2e
```

E2E 会启动独立的本地 API 与 Web 服务，使用临时测试数据库；后端依赖和原生扩展需预先安装。仓库根目录也提供 `make build` 与 `make e2e`。

CI 运行 Web 构建与单测，浏览器 E2E 尚未自动接入。检查结果、测试数量与跳过原因以实际输出为准。

## 当前界面范围

- **会话与分支**：Workspace → Conversation → Lane，持久分支与隔离的临时对话，主 / 侧聊天与会话面板。
- **聊天与恢复**：流式回复、停止、重试、多回答版本、Markdown / 代码展示。预算硬停后，仅安全判据满足时提供同一运行的续跑；失败或取消不会通过此入口复活。
- **文件与变更**：文本附件、`@` 工作区文件引用、目录树、预览和编辑、本会话 Agent 文件变更 diff。
- **执行过程**：运行进度、预算、审批与子运行结果；同一 Conversation 最多一个活动运行。
- **记忆与成果**：提案确认、记忆管理、成果来源、版本、回滚和导出。
- **任务与提醒**：提案与手工创建、任务编辑、运行记录、暂停 / 恢复 / 取消、结果通知。
- **配置**：Provider / Model、Skill、MCP 服务器与工具白名单。

## 运行边界

- 浏览器面板仍为占位视图，没有完整网页浏览能力。
- API 与开发 Web 仅支持 loopback；Vite 开发代理不承担正式生产 Web 托管，本仓库尚未提供桌面安装包。
- 原生文件工具不能代表网页保存及其他文件入口都具备相同的内核边界；shell 的沙箱与审批以实际后端配置为准。
- 普通聊天不创建用户可见 Task；临时探索不默认写入长期记忆。
- 人工产品验收、真实键盘 / 读屏及移动设备验收仍待完成，自动化结果不能替代发布验收。
