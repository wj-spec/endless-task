# Endless Task Web

`@endless-task/web` 是 Endless Task 的本地聊天前端，当前冻结节点为 `0.4.9 / P0-P4 completed`。

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

打开 `http://127.0.0.1:5173`。开发服务器会把 `/health`、`/conversations` 和 `/turns` 等请求代理到 `http://127.0.0.1:8000`。

后端默认使用 `FakeProvider`。连接 DeepSeek 等 OpenAI-compatible 服务时，只在 API 进程中配置密钥，密钥不得进入前端环境变量。

## 构建检查

当前冻结验证结果：`npm run build` 通过，生成生产构建产物。

```bash
npm run build
```

## 当前界面范围

- 会话列表：新建、切换、搜索、重命名、归档、恢复和删除会话。
- 聊天流：发送消息、SSE 流式回复、停止、失败重试、重新生成、多回答版本切换、Markdown 与代码块展示。
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
- P4.5 会话分支、P5 知识源、P6 Skill/MCP、P7 桌面端仍未进入当前实现。
