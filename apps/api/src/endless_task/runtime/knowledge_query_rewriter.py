"""R5.7 可选 LLM query 改写：注入前把用户消息改写成检索式 query。

默认关闭；开启后失败/超时/输出异常一律静默回退原文，不阻塞主链路。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM_PROMPT = (
    "你是检索 query 改写器。把用户的最新消息改写成一条适合在个人知识库中检索的查询，"
    "保留关键实体与术语，去掉寒暄与语气词，不超过 60 字。"
    "只输出改写后的查询本身，不要解释、不要引号。"
    "如果原消息不适合检索（如寒暄、纯指令），原样输出原消息。"
)

_MIN_QUERY_CHARS = 4
_MAX_OUTPUT_CHARS = 200


class KnowledgeQueryRewriter:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str,
        timeout_seconds: float = 6.0,
        max_output_tokens: int = 128,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    async def rewrite(self, query: str) -> Optional[str]:
        """返回改写结果；任何异常或无效输出返回 None（调用方回退原文）。"""
        cleaned = (query or "").strip()
        if len(cleaned) < _MIN_QUERY_CHARS:
            return None
        request = ProviderRequest(
            request_id=f"krw_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_REWRITE_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=cleaned[:2000]),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        try:
            return await asyncio.wait_for(
                self._collect(request), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.debug("Knowledge query rewrite timed out; falling back.")
            return None
        except Exception:  # noqa: BLE001 改写失败必须静默回退
            logger.debug("Knowledge query rewrite failed; falling back.", exc_info=True)
            return None

    async def _collect(self, request: ProviderRequest) -> Optional[str]:
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        rewritten = "".join(chunks).strip().strip('"').strip()
        if not rewritten or len(rewritten) > _MAX_OUTPUT_CHARS:
            return None
        return rewritten
