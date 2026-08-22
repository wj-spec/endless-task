# Endless Task Web

R0.5 的本地聊天前端。界面只呈现 Assistant、Conversation 与消息流，不向用户暴露 Task、Agent Run 或工作流概念。

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

打开 `http://127.0.0.1:5173`。开发服务器会把 `/health`、`/conversations` 和 `/turns` 请求代理到 `http://127.0.0.1:8000`。

后端默认使用 `FakeProvider`。连接 DeepSeek 等 OpenAI-compatible 服务时，只在 API 进程中配置密钥，密钥不得进入前端环境变量。

## 构建检查

```bash
npm run build
```

## R0.5 范围

- 新建、切换、搜索、重命名、归档、恢复和删除会话。
- 发送消息并消费统一 SSE 事件。
- 流式回复、停止、失败重试和重新生成。
- 多回答版本切换。
- Markdown 与代码块展示。
- 桌面与移动尺寸的响应式布局。
- 切换会话时按 Turn 隔离流式状态。

界面采用连续消息流和轻量分隔线，避免消息气泡、卡片堆叠、阴影与常驻工作区。
