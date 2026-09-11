.PHONY: help install api web test e2e build eval

help:
	@echo "Endless Task — 常用命令"
	@echo ""
	@echo "  make install   安装后端与前端依赖"
	@echo "  make api       启动后端 API（默认 FakeProvider，无需 API Key）"
	@echo "  make web       启动前端开发服务（另开一个终端）"
	@echo "  make test      运行后端测试 + 前端单测"
	@echo "  make e2e       运行浏览器 E2E（Playwright）"
	@echo "  make build     前端生产构建"
	@echo "  make eval      跑一轮确定性评估（需要已有录制的 Run）"

install:
	cd apps/api && uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
	cd apps/web && npm install

api:
	cd apps/api && uv run endless-task-api

web:
	cd apps/web && npm run dev

test:
	cd apps/api && uv run python -W error -m unittest discover -s tests
	cd apps/web && npm run test:unit

e2e:
	cd apps/web && npm run test:e2e

build:
	cd apps/web && npm run build

eval:
	cd apps/api && uv run endless-task eval run --require-tools
