"""Task run review (R4.6).

Detects whether a completed automatic turn ended by asking the user for
something (information, materials, authorization). The run journal marks such
runs as awaiting user so the "已安排" overlay can route the user back in.
Review failures must never affect the run itself.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM_PROMPT = (
    "你是执行审查助手。阅读一次自动执行的用户消息与 Assistant 回复，"
    "判断该回复是否要求用户先处理（补充信息、提供材料、授权确认等）"
    "才能完成原本承诺的事项。"
    "已完成承诺或单纯汇报结果时输出 false。"
    "只输出 JSON：{\"awaiting_user\": true|false, "
    "\"note\": \"40字以内理由或null\"}"
)

_MAX_NOTE_CHARS = 80


@dataclass(frozen=True)
class RunReview:
    awaiting_user: bool
    note: Optional[str] = None


class TaskRunReviewService:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        model: str,
        max_output_tokens: int = 300,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def review(
        self, *, user_message: str, assistant_message: str
    ) -> RunReview:
        try:
            return await self._review(
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:  # noqa: BLE001 - review must never fail a run
            logger.warning("Task run review skipped: %s", error)
            return RunReview(False)

    async def _review(
        self, *, user_message: str, assistant_message: str
    ) -> RunReview:
        transcript = (
            f"用户：{user_message.strip()}\nAssistant：{assistant_message.strip()}"
        )
        request = ProviderRequest(
            request_id=f"runrev_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_REVIEW_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=transcript),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        return self._parse("".join(chunks))

    @staticmethod
    def _parse(raw: str) -> RunReview:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return RunReview(False)
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return RunReview(False)
        if not isinstance(payload, dict):
            return RunReview(False)
        awaiting = payload.get("awaiting_user") is True
        note = payload.get("note")
        normalized = note.strip()[:_MAX_NOTE_CHARS] if isinstance(note, str) and note.strip() else None
        return RunReview(awaiting, normalized)
