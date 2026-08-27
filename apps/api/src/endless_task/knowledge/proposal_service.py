from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Sequence, Tuple

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
    '"content":"规范化后的资料文本","reason":"为什么值得长期保留",'
    '"global":true（仅当内容明显是关于用户本人生活的知识时输出，否则省略该字段）}'
    '或{"type":"expire_source","source_id":"已有知识 id","reason":"为什么过时"}]}；'
    "没有合适内容时输出 {\"proposals\":[]}。"
    "归属说明：关于用户本人生活的知识（个人习惯、生活偏好、家庭规范，"
    "与工作/项目无关）才标 global；工作、项目、领域知识一律不标。"
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
        chat_repository=None,
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
        self._chat_repository = chat_repository
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
                title = str(item.get("title", "")).strip() or content[:30]
                reason = str(item.get("reason", "")).strip()
                payload = {
                    "title": title,
                    "content": content,
                    "reason": reason,
                    "workspace_id": self._attribute_workspace(
                        conversation_id,
                        marked_global=bool(item.get("global")),
                        text=f"{title}\n{content}\n{reason}",
                    ),
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

    #: 保守兜底：模型标了 global 但文本缺少「关于用户本人」的信号时，降级回工作区。
    _PERSONAL_SIGNALS = (
        "用户", "我个人", "我自己", "我家", "家里", "生活", "习惯",
        "偏好", "喜欢", "讨厌", "过敏", "每天", "每周", "每月", "总是",
    )

    def _attribute_workspace(
        self, conversation_id: str, *, marked_global: bool, text: str
    ) -> Optional[str]:
        """R5.11 归属判定：拿不准就落工作区，全局宁缺毋滥。

        - 来源会话无工作区（通用会话）→ 全局（提取 prompt 已约束只收关于用户本人的内容）；
        - 会话属于工作区且模型标 global 且文本有个人信号 → 全局；
        - 其余 → 该工作区。
        """
        conversation_workspace: Optional[str] = None
        if self._chat_repository is not None:
            try:
                conversation = self._chat_repository.get_conversation(
                    conversation_id
                )
                conversation_workspace = conversation.workspace_id
            except Exception:  # noqa: BLE001 归属失败退回默认分区
                conversation_workspace = None
        if conversation_workspace is None:
            return None
        if marked_global and any(
            signal in text for signal in self._PERSONAL_SIGNALS
        ):
            return None
        return conversation_workspace

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
