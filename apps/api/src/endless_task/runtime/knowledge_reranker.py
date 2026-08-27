"""R5.9 可选 LLM 重排：注入前按语义相关度对检索候选重排（top-10 → top-3）。

默认关闭；失败/超时/输出异常一律静默回退原排序，不阻塞主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import List, Optional, Sequence

from endless_task.domain.models import KnowledgeHit
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)

logger = logging.getLogger(__name__)

_RERANK_SYSTEM_PROMPT = (
    "你是检索结果相关性排序器。根据用户查询，从候选资料中挑出对回答最有帮助的条目，"
    "并按相关度从高到低排序。只输出候选编号的 JSON 数组（最相关在前），例如 [3,1]。"
    "最多保留 3 项，与查询无关的条目一律剔除，不要输出任何解释。"
)

_JSON_ARRAY_RE = re.compile(r"\[[^\]]*\]")
_MAX_SNIPPET_CHARS = 200


class KnowledgeReranker:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str,
        candidate_limit: int = 10,
        keep: int = 3,
        timeout_seconds: float = 8.0,
        max_output_tokens: int = 64,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if candidate_limit <= 0 or keep <= 0:
            raise ValueError("candidate_limit and keep must be positive")
        self._provider = provider
        self._model = model
        self.candidate_limit = candidate_limit
        self._keep = keep
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    async def rerank(
        self, query: str, candidates: Sequence[KnowledgeHit]
    ) -> Optional[List[str]]:
        """返回按相关度排序的 ref_id 列表；任何异常或无效输出返回 None。"""
        cleaned = (query or "").strip()
        if not cleaned or not candidates:
            return None
        pool = list(candidates[: self.candidate_limit])
        lines = []
        for index, hit in enumerate(pool, start=1):
            snippet = (hit.snippet or hit.title or "").strip()
            if len(snippet) > _MAX_SNIPPET_CHARS:
                snippet = snippet[:_MAX_SNIPPET_CHARS] + "…"
            lines.append(f"{index}. {hit.title or '未命名'}：{snippet}")
        user_content = (
            f"查询：{cleaned[:500]}\n\n候选资料：\n" + "\n".join(lines)
        )
        request = ProviderRequest(
            request_id=f"krk_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_RERANK_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=user_content),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        try:
            raw = await asyncio.wait_for(
                self._collect(request), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.debug("Knowledge rerank timed out; keeping original order.")
            return None
        except Exception:  # noqa: BLE001 重排失败必须静默回退
            logger.debug("Knowledge rerank failed; keeping original order.", exc_info=True)
            return None
        return self._parse(raw, pool)

    def _parse(
        self, raw: Optional[str], pool: Sequence[KnowledgeHit]
    ) -> Optional[List[str]]:
        if raw is None:
            return None
        match = _JSON_ARRAY_RE.search(raw)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        ordering: List[str] = []
        seen: set = set()
        for item in parsed:
            if isinstance(item, bool) or not isinstance(item, int):
                continue
            index = item - 1
            if 0 <= index < len(pool):
                ref_id = pool[index].ref_id
                if ref_id not in seen:
                    seen.add(ref_id)
                    ordering.append(ref_id)
        if not ordering:
            return None
        return ordering[: self._keep]

    async def _collect(self, request: ProviderRequest) -> Optional[str]:
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        return "".join(chunks).strip()
