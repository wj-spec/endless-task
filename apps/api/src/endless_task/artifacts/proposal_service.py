from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Tuple

from endless_task.domain.models import (
    ArtifactKind,
    ArtifactProposal,
    ArtifactStatus,
    MemoryStatus,
)
from endless_task.domain.repositories import RepositoryError
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage.sqlite_artifact_proposal_repository import (
    SqliteArtifactProposalRepository,
)

from .source_labels import (
    MAX_SOURCE_LABELS,
    format_file_label,
    format_memory_label,
    parse_source_label,
)

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是 Artifact 提取助手。阅读一段用户与 Assistant 的对话，"
    "判断是否产生 Artifact 提案："
    "1. 当 Assistant 的回答属于值得整体保留的独立成文结果"
    "（长回答、计划、改写稿、报告、总结等）时，提出创建提案。"
    "2. 当用户要求修改随附 Artifact 清单中的某一份，且 Assistant 的回答给出"
    "完整修改后的文档时，提出更新提案，并把 target 设为该 Artifact 的 id。"
    "问答片段、列表式简短说明、讨论中的中间回答不要提案。"
    "提案内容必须是可独立阅读的完整文档，不得只给差量或片段，不要包含对话措辞。"
    "如果回答明确引用了随附长期记忆清单中的某些记忆，"
    '在 labels 中以 "memory:{id}" 形式列出对应记忆；不要输出其他形式的标签。'
    '只输出 JSON：{"proposal":{"title":"文档标题","kind":"markdown|text",'
    '"content":"完整文档内容","reason":"为什么值得保留","labels":[],'
    '"target":"art_xxx 或省略"}}；没有合适内容时输出 {"proposal":null}。'
)

DEFAULT_PROPOSAL_REASON = "回答较长且值得保留为独立文档"
READ_TEXT_FILE_TOOL = "read_text_file"
COMPLETED_STATUS = "completed"
MEMORY_LIST_CAP = 20
MEMORY_PROMPT_SNIPPET_CHARS = 200
ARTIFACT_LIST_CAP = 10
ARTIFACT_TARGET_PREFIX = "artifact:"


class ArtifactProposalService:
    """Generates pending artifact proposals from completed turns.

    The service never writes to ``artifacts``; writing happens only when the
    user accepts a proposal through the resolve API. Proposals either create a
    new artifact (R3.1) or target an existing one for a ``chat_continue``
    version (R3.4).
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        proposal_repository: SqliteArtifactProposalRepository,
        model: str,
        memory_repository=None,
        runtime_repository=None,
        artifact_repository=None,
        min_assistant_chars: int = 400,
        max_output_tokens: int = 8_000,
    ) -> None:
        if min_assistant_chars <= 0:
            raise ValueError("min_assistant_chars must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._model = model
        self._memory_repository = memory_repository
        self._runtime_repository = runtime_repository
        self._artifact_repository = artifact_repository
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
        transcript = (
            f"用户：{user_message.strip()}\nAssistant：{stripped_answer}"
            f"{self._memory_list_block()}{self._artifact_list_block()}"
        )
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

        target_artifact_id = None
        base_version_ordinal: Optional[int] = None
        inherited_labels: Tuple[str, ...] = ()
        raw_target = payload.get("target")
        if raw_target is not None:
            resolved = self._resolve_target(raw_target)
            if resolved is None:
                return ()
            (
                target_artifact_id,
                current_content,
                inherited_labels,
                base_version_ordinal,
            ) = resolved
            if content.strip() == current_content:
                return ()

        labels = self._assemble_labels(
            turn_id, payload.get("labels"), inherited_labels
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
                source_labels=labels,
                target_artifact_id=target_artifact_id,
                base_version_ordinal=base_version_ordinal,
            )
        except RepositoryError:
            return ()
        return (proposal,)

    def _resolve_target(
        self, raw_target
    ) -> Optional[Tuple[str, str, Tuple[str, ...], int]]:
        if self._artifact_repository is None or not isinstance(raw_target, str):
            return None
        candidate = raw_target.strip()
        if candidate.startswith(ARTIFACT_TARGET_PREFIX):
            candidate = candidate[len(ARTIFACT_TARGET_PREFIX):]
        if not candidate:
            return None
        try:
            artifact = self._artifact_repository.get_artifact(candidate)
        except RepositoryError:
            return None
        if artifact.status is not ArtifactStatus.ACTIVE:
            return None
        current_version = self._artifact_repository.get_current_version(candidate)
        return (
            candidate,
            current_version.content.strip(),
            current_version.source_labels,
            current_version.ordinal,
        )

    def _assemble_labels(
        self,
        turn_id: str,
        model_labels,
        inherited_labels: Tuple[str, ...] = (),
    ) -> Tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        candidates = (
            list(inherited_labels)
            + self._file_labels_for_turn(turn_id)
            + self._validated_memory_labels(model_labels)
        )
        for label in candidates:
            if label in seen:
                continue
            seen.add(label)
            ordered.append(label)
        return tuple(ordered[:MAX_SOURCE_LABELS])

    def _file_labels_for_turn(self, turn_id: str) -> list[str]:
        if self._runtime_repository is None:
            return []
        labels: list[str] = []
        for call in self._runtime_repository.list_tool_calls(turn_id):
            if call.tool_name != READ_TEXT_FILE_TOOL:
                continue
            if call.status != COMPLETED_STATUS:
                continue
            file_id = call.arguments.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                continue
            labels.append(format_file_label(file_id.strip()))
        return labels

    def _validated_memory_labels(self, model_labels) -> list[str]:
        if self._memory_repository is None or not isinstance(model_labels, list):
            return []
        labels: list[str] = []
        for item in model_labels:
            parsed = parse_source_label(item if isinstance(item, str) else "")
            if parsed.kind != "memory":
                continue
            try:
                record = self._memory_repository.get_memory(parsed.target_id)
            except RepositoryError:
                continue
            if record.status is not MemoryStatus.ACTIVE:
                continue
            labels.append(format_memory_label(parsed.target_id))
        return labels

    def _memory_list_block(self) -> str:
        if self._memory_repository is None:
            return ""
        lines: list[str] = []
        for record in self._memory_repository.list_memories()[:MEMORY_LIST_CAP]:
            snippet = record.content.strip().replace("\n", " ")[
                :MEMORY_PROMPT_SNIPPET_CHARS
            ]
            lines.append(f"- memory:{record.id}: {snippet}")
        if not lines:
            return ""
        return "\n\n当前可用的长期记忆清单：\n" + "\n".join(lines)

    def _artifact_list_block(self) -> str:
        if self._artifact_repository is None:
            return ""
        records = self._artifact_repository.list_artifacts()[:ARTIFACT_LIST_CAP]
        if not records:
            return ""
        lines = [
            f"- artifact:{record.id}: 《{record.title}》"
            f"（{record.kind.value}）v{record.current_version_ordinal}"
            for record in records
        ]
        return "\n\n当前已保存的 Artifact 清单：\n" + "\n".join(lines)

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
