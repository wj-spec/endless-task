from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Tuple

from endless_task.domain.models import ArtifactKind, ArtifactProposal
from endless_task.domain.repositories import RepositoryError
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage import SqliteArtifactProposalRepository

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是 Artifact 提取助手。阅读一段用户与 Assistant 的对话，"
    "仅当 Assistant 的回答属于值得整体保留的独立成文结果"
    "（长回答、计划、改写稿、报告、总结等）时，才提出 Artifact 提案。"
    "问答片段、列表式简短说明、讨论中的中间回答不要提案。"
    "提案内容应整理为可独立阅读的文档，不要包含对话措辞。"
    '只输出 JSON：{"proposal":{"title":"文档标题","kind":"markdown|text",'
    '"content":"文档内容","reason":"为什么值得保留"}}；'
    '没有合适内容时输出 {"proposal":null}。'
)

DEFAULT_PROPOSAL_REASON = "回答较长且值得保留为独立文档"


class ArtifactProposalService:
    """Generates pending artifact proposals from completed turns.

    The service never writes to ``artifacts``; writing happens only when the
    user accepts a proposal through the resolve API.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        proposal_repository: SqliteArtifactProposalRepository,
        model: str,
        min_assistant_chars: int = 400,
        max_output_tokens: int = 8_000,
    ) -> None:
        if min_assistant_chars <= 0:
            raise ValueError("min_assistant_chars must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._model = model
        self._min_assistant_chars = min_assistant_chars
        self._max_output_tokens = max_output_tokens

    async def generate_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[ArtifactProposal, ...]:
        try:
            return await self._generate(
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:  # noqa: BLE001 - proposal generation must never fail a turn
            logger.warning(
                "Artifact proposal generation skipped for turn %s: %s",
                turn_id,
                error,
            )
            return ()

    async def _generate(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[ArtifactProposal, ...]:
        stripped_answer = assistant_message.strip()
        if len(stripped_answer) < self._min_assistant_chars:
            return ()
        transcript = f"用户：{user_message.strip()}\nAssistant：{stripped_answer}"
        request = ProviderRequest(
            request_id=f"artp_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=transcript),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        payload = self._parse_proposal("".join(chunks))
        if payload is None:
            return ()

        title = payload.get("title")
        content = payload.get("content")
        if not isinstance(title, str) or not title.strip():
            return ()
        if not isinstance(content, str) or not content.strip():
            return ()
        kind = self._parse_kind(payload.get("kind"))
        if kind is None:
            return ()
        reason = payload.get("reason")
        normalized_reason = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else DEFAULT_PROPOSAL_REASON
        )

        if (
            self._proposal_repository.find_pending_by_content(content.strip())
            is not None
        ):
            return ()
        try:
            proposal = self._proposal_repository.create_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                title=title,
                kind=kind,
                content=content,
                reason=normalized_reason,
            )
        except RepositoryError:
            return ()
        return (proposal,)

    @staticmethod
    def _parse_proposal(raw: str) -> Optional[dict]:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        item = payload.get("proposal")
        return item if isinstance(item, dict) else None

    @staticmethod
    def _parse_kind(value: object) -> Optional[ArtifactKind]:
        if isinstance(value, ArtifactKind):
            return value
        try:
            return ArtifactKind(str(value))
        except ValueError:
            return None
