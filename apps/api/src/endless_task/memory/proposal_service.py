from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Sequence, Tuple

from endless_task.domain.models import MemoryKind, MemoryProposal
from endless_task.domain.repositories import RepositoryError
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage import (
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是记忆提取助手。阅读一段用户与 Assistant 的对话，"
    "仅当用户明确表达了值得长期保留的偏好（preference）或事实（fact）时，"
    "才提出记忆提案。不要推断、猜测或提取文件内容。"
    "随附“待确认/已记住清单”：与已记住语义相同的不要再提案；"
    "与待确认语义相同的照常输出新提案，并把旧提案 id 填入该条目的 supersedes"
    "（系统会取消旧提案、保留新提案）。"
    '只输出 JSON：{"proposals":[{"kind":"preference|fact",'
    '"content":"规范化后的记忆文本","reason":"为什么值得记住",'
    '"supersedes":"旧提案 id 或省略"}]}；'
    "没有合适内容时输出 {\"proposals\":[]}。"
)

PENDING_LIST_CAP = 10
ACTIVE_LIST_CAP = 20
LIST_SNIPPET_CHARS = 200


class MemoryProposalService:
    """Generates pending memory proposals from completed turns.

    The service never writes to ``memories``; confirmation belongs to R2.2.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        proposal_repository: SqliteMemoryProposalRepository,
        memory_repository: SqliteMemoryRepository,
        model: str,
        max_proposals_per_turn: int = 2,
        max_output_tokens: int = 600,
    ) -> None:
        if max_proposals_per_turn <= 0:
            raise ValueError("max_proposals_per_turn must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._memory_repository = memory_repository
        self._model = model
        self._max_proposals_per_turn = max_proposals_per_turn
        self._max_output_tokens = max_output_tokens

    async def generate_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[MemoryProposal, ...]:
        try:
            return await self._generate(
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:  # noqa: BLE001 - proposal generation must never fail a turn
            logger.warning(
                "Memory proposal generation skipped for turn %s: %s",
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
    ) -> Tuple[MemoryProposal, ...]:
        transcript = (
            f"用户：{user_message.strip()}\nAssistant：{assistant_message.strip()}"
            f"{self._awareness_block(conversation_id)}"
        )
        request = ProviderRequest(
            request_id=f"memp_{uuid.uuid4().hex}",
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

        created: list[MemoryProposal] = []
        active_contents = {
            record.content for record in self._memory_repository.list_memories()
        }
        for item in self._parse_proposals(raw):
            if len(created) >= self._max_proposals_per_turn:
                break
            content = str(item.get("content", "")).strip()
            if not content or content in active_contents:
                continue
            try:
                proposal = self._proposal_repository.create_proposal(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    kind=self._parse_kind(item.get("kind")),
                    content=content,
                    reason=str(item.get("reason", "")).strip() or "用户对话中明确表达",
                )
            except RepositoryError:
                continue
            self._supersede(conversation_id, proposal, item.get("supersedes"))
            created.append(proposal)
        return tuple(created)

    def _supersede(
        self,
        conversation_id: str,
        created: MemoryProposal,
        supersedes: object,
    ) -> None:
        stale_ids: list[str] = []
        if isinstance(supersedes, str) and supersedes.strip():
            candidate = supersedes.strip()
            try:
                referenced = self._proposal_repository.get_proposal(candidate)
            except RepositoryError:
                referenced = None
            if (
                referenced is not None
                and referenced.conversation_id == conversation_id
                and referenced.status.value == "pending"
            ):
                stale_ids.append(candidate)
        for pending in self._proposal_repository.list_proposals(
            conversation_id=conversation_id
        ):
            if pending.id == created.id:
                continue
            if pending.content.strip() == created.content.strip():
                stale_ids.append(pending.id)
        for stale_id in dict.fromkeys(stale_ids):
            try:
                self._proposal_repository.cancel_proposal(stale_id)
            except RepositoryError:
                continue

    def _awareness_block(self, conversation_id: str) -> str:
        lines: list[str] = []
        for proposal in self._proposal_repository.list_proposals(
            conversation_id=conversation_id
        )[:PENDING_LIST_CAP]:
            snippet = proposal.content.strip().replace("\n", " ")[
                :LIST_SNIPPET_CHARS
            ]
            lines.append(f"- 待确认 {proposal.id}：{snippet}")
        for record in self._memory_repository.list_memories()[:ACTIVE_LIST_CAP]:
            snippet = record.content.strip().replace("\n", " ")[
                :LIST_SNIPPET_CHARS
            ]
            lines.append(f"- 已记住：{snippet}")
        if not lines:
            return ""
        return (
            "\n以下是待确认或已记住的内容：与已记住语义相同的不要再提案；"
            "与待确认语义相同的照常输出新提案，并把对应旧 id 填入 supersedes。\n"
            + "\n".join(lines)
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
    def _parse_kind(value: Optional[object]) -> MemoryKind:
        if isinstance(value, MemoryKind):
            return value
        return MemoryKind(str(value))
