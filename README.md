# Endless Task

Endless Task 是一个本地优先、面向单用户的个人 AI 助手。产品首先是一款类似 ChatGPT 的聊天应用；Task、工具和长期执行能力将在后续由 Assistant 按需使用，而不是成为用户的前置操作。

## 当前状态

P0 已完成：本地聊天闭环、OpenAI-compatible Provider、真实 Web 前端、上下文管理，以及基础可靠性与本地安全能力均已通过发布验收。

P1 已通过自动化发布验收（人工确认进行中）：工具协议、受限 Agent Loop、首个会话级只读文件工具、一次性确认链路、聊天内轻量 Activity 与 P1 发布门槛自动化验收均已通过。用户可以在聊天中附加本地文本文件；工具运行只显示自然状态，工具失败时由 Assistant 说明原因和下一步，未来的写操作或外部操作必须明确批准，产品不增加 Chat / Work 模式。

## 本地运行

需要 Python 3.9+、[uv](https://docs.astral.sh/uv/) 和 Node.js。

```bash
cd apps/api
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
uv run endless-task-api
```

另开终端：

```bash
cd apps/web
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。后端默认使用 FakeProvider，无需 API Key。

配置 DeepSeek、数据位置和备份方式见 [API 说明](apps/api/README.md)，产品范围与阶段见 [开发路线图](docs/product/development-roadmap.md)。
