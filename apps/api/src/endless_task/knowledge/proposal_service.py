from __future__ import annotations

import json
import logging
import uuid
from typing import Sequence, Tuple

from endless_task.domain.models import (
    KnowledgeProposal,
    KnowledgeProposalType,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import RepositoryError
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage import (
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
)

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是知识管理助手。阅读一段用户与 Assistant 的对话，"
    "仅当对话中出现明显具有长期复用价值的信息（规范、流程、偏好性资料等）时，"
    "才提出 add_source 提案；仅当用户明确表示某条已有知识已过时或作废时，"
    "才提出 expire_source 提案。不要推断、猜测，不要提议一次性信息。"
    "随附“待确认/已有知识清单”：与已有知识语义相同的不要再提案；"
    "expire 的 source_id 必须取自清单中活跃知识的 id。"
    '只输出 JSON：{"proposals":[{"type":"add_source","title":"简短标题",'
    '"content":"规范化后的资料文本","reason":"为什么值得长期保留"}'
    '或{"type":"expire_source","source_id":"已有知识 id","reason":"为什么过时"}]}；'
    "没有合适内容时输出 {\"proposals\":[]}。"
)

PENDING_LIST_CAP = 10
ACTIVE_LIST_CAP = 20
LIST_SNIPPET_CHARS = 120


class KnowledgeProposalService:
    """Generates pending knowledge proposals from completed turns.

    The service never writes to ``knowledge_sources``; confirmation belongs
    to the proposal resolve flow.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        proposal_repository: SqliteKnowledgeProposalRepository,
        knowledge_repository: SqliteKnowledgeRepository,
        model: str,
        max_proposals_per_turn: int = 2,
        max_output_tokens: int = 600,
        min_assistant_chars: int = 40,
    ) -> None:
        if max_proposals_per_turn <= 0:
            raise ValueError("max_proposals_per_turn must be positive")
        if min_assistant_chars <= 0:
            raise ValueError("min_assistant_chars must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._knowledge_repository = knowledge_repository
        self._model = model
        self._max_proposals_per_turn = max_proposals_per_turn
        self._max_output_tokens = max_output_tokens
        self._min_assistant_chars = min_assistant_chars

    async def generate_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[KnowledgeProposal, ...]:
        try:
            return await self._generate(
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:  # noqa: BLE001 - proposal generation must never fail a turn
            logger.warning(
                "Knowledge proposal generation skipped for turn %s: %s",
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
    ) -> Tuple[KnowledgeProposal, ...]:
        if len(assistant_message.strip()) < self._min_assistant_chars:
            return ()
        transcript = (
            f"用户：{user_message.strip()}\nAssistant：{assistant_message.strip()}"
            f"{self._awareness_block()}"
        )
        request = ProviderRequest(
            request_id=f"knwp_{uuid.uuid4().hex}",
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
        raw = "".join(chunks)

        active_sources = self._knowledge_repository.list_sources()
        active_contents = {record.content for record in active_sources}
        active_titles = {record.id: record.title for record in active_sources}

        created: list[KnowledgeProposal] = []
        for item in self._parse_proposals(raw):
            if len(created) >= self._max_proposals_per_turn:
                break
            proposal_type = self._parse_type(item.get("type"))
            if proposal_type is None:
                continue
            if proposal_type is KnowledgeProposalType.ADD_SOURCE:
                content = str(item.get("content", "")).strip()
                if not content or content in active_contents:
                    continue
                payload = {
                    "title": str(item.get("title", "")).strip() or content[:30],
                    "content": content,
                    "reason": str(item.get("reason", "")).strip(),
                }
            else:
                source_id = str(item.get("source_id", "")).strip()
                if not source_id or source_id not in active_titles:
                    continue
                payload = {
                    "source_id": source_id,
                    "title": active_titles[source_id],
                    "reason": str(item.get("reason", "")).strip(),
                }
            try:
                proposal = self._proposal_repository.create_proposal(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    proposal_type=proposal_type,
                    payload=payload,
                )
            except RepositoryError:
                continue
            created.append(proposal)
        return tuple(created)

    def _awareness_block(self) -> str:
        lines: list[str] = []
        for proposal in self._proposal_repository.list_pending(limit=PENDING_LIST_CAP):
            snippet = str(proposal.payload.get("content", "")).strip().replace(
                "\n", " "
            )[:LIST_SNIPPET_CHARS]
            lines.append(f"- 待确认新增：{snippet}")
        for record in self._knowledge_repository.list_sources()[:ACTIVE_LIST_CAP]:
            snippet = record.content.strip().replace("\n", " ")[
                :LIST_SNIPPET_CHARS
            ]
            lines.append(f"- 活跃知识 {record.id}《{record.title}》：{snippet}")
        if not lines:
            return ""
        return (
            "\n以下是待确认或已有的知识：与已有知识语义相同的不要再提案；"
            "expire_source 的 source_id 必须取自活跃知识 id。\n" + "\n".join(lines)
        )

    def _parse_proposals(self, raw: str) -> Sequence[dict]:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return ()
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return ()
        items = payload.get("proposals") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return ()
        return tuple(item for item in items if isinstance(item, dict))

    @staticmethod
    def _parse_type(value: object) -> KnowledgeProposalType | None:
        try:
            return KnowledgeProposalType(str(value))
        except ValueError:
            return None
