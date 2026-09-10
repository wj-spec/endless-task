"""知识源去重触发（被 knowledge-source 路由与 knowledge-proposal 路由共用）。

原本是 create_app 的闭包函数，隐式依赖 `container`；现在做成模块级函数并显式
接收 `container`，两处调用方都能导入，也不会形成 routes → app 的环。
行为零改动（异常同样被吞掉，只记 debug 日志）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def emit_knowledge_duplicates(container, source) -> None:
    service = container.knowledge_lifecycle_service
    if service is None:
        return
    try:
        service.detect_duplicates(source)
    except Exception:  # noqa: BLE001 去重检测不影响写入本身
        logger.debug("Knowledge duplicate detection failed", exc_info=True)
