from __future__ import annotations

from typing import Callable

import json
import logging
import uuid
from typing import Mapping, Optional, Sequence, Tuple

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
from endless_task.storage.sqlite_memory_repository import AUTO_FACT_ORIGIN

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是记忆提取助手，提取阈值极严，宁可漏记不可错记。阅读一段用户与 Assistant 的对话，"
    "仅当用户明确、主动陈述了关于其自身的长期偏好（preference）或事实（fact）时"
    "（如「记住我喜欢猫」「我在上海工作」），才提出记忆提案。以下情况一律不提案："
    "一次性任务信息、当前请求与临时状态；领域规范、工作流程等关于「事」的知识"
    "（它们属于知识通道，不属于记忆通道）；Assistant 的推断、猜测或总结；用户未明确陈述的内容。"
    "不要推断、猜测或提取文件内容。"
    "随附“待确认/已记住清单”：与已记住语义相同的不要再提案；"
    "与待确认语义相同的照常输出新提案，并把旧提案 id 填入该条目的 supersedes"
    "（系统会取消旧提案、保留新提案）。"
    "confidence 表示该陈述的明确程度：用户直接、明确陈述为 high；较明确但存在推断空间"
    "为 medium；含糊或仅间接提示为 low。"
    '只输出 JSON：{"proposals":[{"kind":"preference|fact",'
    '"content":"规范化后的记忆文本","reason":"为什么值得记住",'
    '"confidence":"low|medium|high",'
    '"supersedes":"旧提案 id 或省略"}]}；'
    "没有合适内容时输出 {\"proposals\":[]}。"
)

# 静默原则：用户消息不含明确记忆意图标记时，直接跳过模型调用。
_MARKER_HINTS = (
    "记住",
    "我喜欢",
    "我爱",
    "我偏好",
    "我的习惯",
    "我一般",
    "我通常",
    "我以后",
    "以后都",
    "每次都",
    "叫我",
    "我叫",
    "我姓",
    "我是",
    "我在",
    "我养",
)


def has_memory_marker(message: str) -> bool:
    return any(hint in message for hint in _MARKER_HINTS)


PENDING_LIST_CAP = 10
ACTIVE_LIST_CAP = 20
LIST_SNIPPET_CHARS = 200

# 自动写入只放行明确度达标的 fact；低明确度一律降级为待确认提案。
_AUTO_FACT_CONFIDENCE = frozenset({"medium", "high"})


class MemoryProposalService:
    """Generates pending memory proposals from completed turns.

    The service never writes to ``memories`` unless ``auto_fact_enabled`` is on:
    fact memories with sufficient extraction confidence are written directly with
    ``write_origin=auto_fact``; preferences and low-confidence facts still require
    confirmation through proposals. Confirmation belongs to R2.2.
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
        marker_gate_enabled: bool = True,
        auto_fact_enabled: bool = False,
    ) -> None:
        if max_proposals_per_turn <= 0:
            raise ValueError("max_proposals_per_turn must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._memory_repository = memory_repository
        self._model = model
        self._max_proposals_per_turn = max_proposals_per_turn
        self._max_output_tokens = max_output_tokens
        self._marker_gate_enabled = marker_gate_enabled
        self._auto_fact_enabled = auto_fact_enabled

    async def generate_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
        auto_write_target: Optional[
            Callable[[MemoryKind, str, str, str], None]
        ] = None,
    ) -> Tuple[MemoryProposal, ...]:
        if self._marker_gate_enabled and not has_memory_marker(user_message):
            return ()
        try:
            return await self._generate(
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
                auto_write_target=auto_write_target,
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
        auto_write_target: Optional[
            Callable[[MemoryKind, str, str, str], None]
        ] = None,
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
            kind = self._parse_kind(item.get("kind"))
            if self._auto_write_fact(kind, item):
                self._auto_write_fact_memory(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    kind=kind,
                    content=content,
                    supersedes=item.get("supersedes"),
                    write_target=auto_write_target,
                )
                continue
            try:
                proposal = self._proposal_repository.create_proposal(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    kind=kind,
                    content=content,
                    reason=str(item.get("reason", "")).strip() or "用户对话中明确表达",
                )
            except RepositoryError:
                continue
            self._supersede(conversation_id, proposal, item.get("supersedes"))
            created.append(proposal)
        return tuple(created)

    def _auto_write_fact(self, kind: MemoryKind, item: Mapping[str, object]) -> bool:
        if not self._auto_fact_enabled or kind is not MemoryKind.FACT:
            return False
        confidence = str(item.get("confidence", "")).strip().lower()
        return confidence in _AUTO_FACT_CONFIDENCE

    def _auto_write_fact_memory(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        kind: MemoryKind,
        content: str,
        supersedes: object,
        write_target: Optional[Callable[[MemoryKind, str, str, str], None]] = None,
    ) -> None:
        if write_target is not None:
            # v2 运行时:自动写入目标由调用方注入(v2 user_global 记忆)。
            try:
                write_target(kind, content, conversation_id, turn_id)
            except Exception:  # noqa: BLE001 自动写入失败不阻断提取
                logger.warning(
                    "Auto-fact memory write failed for conversation %s: %s",
                    conversation_id,
                    content,
                )
                return
        else:
            try:
                self._memory_repository.create_memory(
                    kind=kind,
                    content=content,
                    source_conversation_id=conversation_id,
                    source_turn_id=turn_id,
                    write_origin=AUTO_FACT_ORIGIN,
                )
            except RepositoryError:
                return
        logger.info(
            "Auto-written fact memory for conversation %s: %s",
            conversation_id,
            content,
        )
        # 自动写入后取消同内容的待确认提案与 supersedes 指向的旧提案，
        # 避免用户再确认一条已经自动记住的内容。
        if isinstance(supersedes, str) and supersedes.strip():
            self._cancel_pending_proposal(conversation_id, supersedes.strip())
        self._cancel_pending_with_content(conversation_id, content)

    def _cancel_pending_proposal(self, conversation_id: str, proposal_id: str) -> None:
        try:
            referenced = self._proposal_repository.get_proposal(proposal_id)
        except RepositoryError:
            return
        if (
            referenced.conversation_id == conversation_id
            and referenced.status.value == "pending"
        ):
            try:
                self._proposal_repository.cancel_proposal(proposal_id)
            except RepositoryError:
                pass

    def _cancel_pending_with_content(self, conversation_id: str, content: str) -> None:
        for pending in self._proposal_repository.list_proposals(
            conversation_id=conversation_id
        ):
            if (
                pending.status.value == "pending"
                and pending.content.strip() == content.strip()
            ):
                try:
                    self._proposal_repository.cancel_proposal(pending.id)
                except RepositoryError:
                    continue

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
