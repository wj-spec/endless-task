.PHONY: help install install-api install-web api web test test-api test-web test-rust verify-native e2e build eval

PYTHON_VERSION ?= 3.12

help:
	@echo "Endless Task — 常用命令"
	@echo ""
	@echo "  make install   安装后端与前端依赖"
	@echo "  make api       启动后端 API（默认 FakeProvider，无需 API Key）"
	@echo "  make web       启动前端开发服务（另开一个终端）"
	@echo "  make test      运行 Rust、后端测试和前端单测"
	@echo "  make test-rust  运行锁定依赖的 Rust workspace 测试"
	@echo "  make verify-native  检查原生扩展可用性与 ABI"
	@echo "  make e2e       运行浏览器 E2E（Playwright）"
	@echo "  make build     前端生产构建"
	@echo "  make eval      跑一轮确定性评估（需要已有录制的 Run）"

install: install-api install-web

install-api:
	cd apps/api && uv sync --locked --python $(PYTHON_VERSION) --extra dev --extra vec
	$(MAKE) verify-native

install-web:
	cd apps/web && npm ci

api:
	cd apps/api && .venv/bin/endless-task-api

web:
	cd apps/web && npm run dev

test: test-rust test-api test-web

test-rust: verify-native
	cd apps/api && PYO3_PYTHON="$$PWD/.venv/bin/python" cargo test --workspace --locked

verify-native:
	cd apps/api && .venv/bin/python ../../scripts/verify_native.py

test-api: verify-native
	cd apps/api && TZ=Asia/Shanghai .venv/bin/python -m unittest discover -s tests

test-web:
	cd apps/web && npm run test:unit

e2e:
	cd apps/web && npm run test:e2e

build:
	cd apps/web && npm run build

eval:
	cd apps/api && .venv/bin/endless-task eval run --require-tools
