from __future__ import annotations

import json
import logging
import uuid
from typing import Sequence, Tuple

from endless_task.domain.models import MemoryRecord, MemoryStatus
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage import SqliteMemoryRepository

logger = logging.getLogger(__name__)

_CONFLICT_SYSTEM_PROMPT = (
    "你是记忆冲突检测助手。给定一条新确认的长期记忆和若干既有记忆，"
    "判断哪些既有记忆已被新记忆取代或矛盾（例如地点、偏好已变更）。"
    "仅当明确矛盾时才列出；包含关系或补充信息不算矛盾。"
    '只输出 JSON：{"superseded": ["mem_x", ...]}；没有时输出 {"superseded": []}。'
)


class MemoryConflictService:
    """Expires active memories contradicted by a newly confirmed memory."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        memory_repository: SqliteMemoryRepository,
        model: str,
        max_candidates: int = 20,
        max_output_tokens: int = 300,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self._provider = provider
        self._memory_repository = memory_repository
        self._model = model
        self._max_candidates = max_candidates
        self._max_output_tokens = max_output_tokens

    async def resolve_conflicts_for(
        self, new_memory: MemoryRecord
    ) -> Tuple[MemoryRecord, ...]:
        try:
            return await self._resolve(new_memory)
        except Exception as error:  # noqa: BLE001 - conflict check must never fail a confirmation
            logger.warning(
                "Memory conflict check skipped for %s: %s",
                new_memory.id,
                error,
            )
            return ()

    async def _resolve(
        self, new_memory: MemoryRecord
    ) -> Tuple[MemoryRecord, ...]:
        candidates = tuple(
            record
            for record in self._memory_repository.list_memories()
            if record.id != new_memory.id
        )[: self._max_candidates]
        if not candidates:
            return ()
        listing = "\n".join(
            f"{record.id}: ({record.kind.value}) {record.content}"
            for record in candidates
        )
        request = ProviderRequest(
            request_id=f"memc_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_CONFLICT_SYSTEM_PROMPT),
                ProviderMessage(
                    role="user",
                    content=(
                        f"新记忆：({new_memory.kind.value}) {new_memory.content}\n"
                        f"既有记忆：\n{listing}"
                    ),
                ),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        expired: list[MemoryRecord] = []
        for memory_id in self._parse_superseded("".join(chunks)):
            target = next(
                (record for record in candidates if record.id == memory_id),
                None,
            )
            if target is None or target.status is not MemoryStatus.ACTIVE:
                continue
            expired.append(
                self._memory_repository.expire_memory(
                    memory_id,
                    reason="superseded",
                    superseded_by=new_memory.id,
                )
            )
        return tuple(expired)

    def _parse_superseded(self, raw: str) -> Sequence[str]:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return ()
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return ()
        ids = payload.get("superseded") if isinstance(payload, dict) else None
        if not isinstance(ids, list):
            return ()
        return tuple(item for item in ids if isinstance(item, str))
