# Endless Task Web

`@endless-task/web` 是 Endless Task 的本地聊天前端，当前包版本为 `0.5.0`，处于 Runtime v2 v1.1 Feature Integration Candidate 阶段，尚不是 Release Candidate。

界面只呈现 Assistant、Conversation 与消息流。Memory、Artifact、Task、提醒和通知都作为对话中的自然结果或辅助面板出现，不向用户暴露 Agent Run、工作流编排或 Chat / Work 模式。

## 本地启动

先启动 API：

```bash
cd apps/api
uv run endless-task-api
```

再启动 Web：

```bash
cd apps/web
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。开发服务器会把 `/api`、`/health`、`/conversations`、`/capabilities`、`/workspaces`、`/providers` 等前端 API 请求代理到 `http://127.0.0.1:8000`。

后端默认使用 `FakeProvider`。连接 DeepSeek 等 OpenAI-compatible 服务时，只在 API 进程中配置密钥，密钥不得进入前端环境变量。

## 构建与浏览器检查

当前开发基线：production build 通过；Playwright 桌面 13、移动端 1，共 14/14 通过。

```bash
npm run build
npm run test:e2e
```

## 当前界面范围

- 会话与分支：Workspace → Conversation → Lane；Branch 在 Conversation 内持久化，Temporary Conversation 为隔离的独立 Conversation。
- 聊天流：发送消息、SSE 流式回复、停止、中断恢复、安全重试、重新生成、多回答版本切换、Markdown 与代码块展示。
- 执行约束：同一 Conversation 最多一个活动 Run；不同 Conversation 可以并行。
- 文件读取：上传本地文本文件，Assistant 按需调用只读工具，界面展示自然 Activity。
- 提案确认：Memory、Artifact、Task 和提醒都通过卡片确认，不用聊天文字替代高风险确认。
- Memory 管理：查看、修改和删除已确认记忆。
- Artifact 工作区：查看成果详情、来源、版本时间线、回滚与导出。
- Task 与提醒：查看任务/提醒、运行记录、暂停、恢复、取消、手动触发和结果通知。
- 权限设置：全局权限模式需要显式确认，可随时收回。

## 交互边界

- 普通聊天不创建用户可见 Task。
- 工具状态只展示自然语言 Activity，不展示原始工具参数、模型思维链或完整日志。
- 当前没有独立工作流编辑器，也不提供手动工具选择器。
- Branch 与 Temporary Conversation 已进入 Runtime v2 v1.1；前者属于原 Conversation，后者复制当前可见路径后完全隔离，不得混用。
- API 和开发 Web 仅支持 loopback；当前 Vite 开发代理不是正式 production Web 托管方案。
- 人工产品验收、真实键盘/读屏与移动设备验收仍未完成，自动化绿色不等于 Release Ready。
