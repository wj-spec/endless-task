from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime.provider import ProviderMessage

from .domain import LaneKind, MemoryScope, RuntimeV2MemoryPromotion, RuntimeV2MemoryRecord

if TYPE_CHECKING:
    from endless_task.storage import (
        SqliteChatRepository,
        SqliteRuntimeV2MemoryRepository,
        SqliteRuntimeV2Repository,
    )


_MEMORY_KINDS = {"preference", "fact"}
_MAX_CONTEXT_MEMORIES = 20
_MAX_CONTEXT_CHARS = 4_000


@dataclass(frozen=True)
class RuntimeV2MemoryQueryResult:
    records: tuple[RuntimeV2MemoryRecord, ...]
    visible_lane_ids: tuple[str, ...]
    workspace_id: Optional[str]


class RuntimeV2MemoryService:
    def __init__(
        self,
        *,
        chat_repository: SqliteChatRepository,
        runtime_repository: SqliteRuntimeV2Repository,
        memory_repository: SqliteRuntimeV2MemoryRepository,
    ) -> None:
        self._chat_repository = chat_repository
        self._runtime_repository = runtime_repository
        self._memory_repository = memory_repository

    def query(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        run_id: Optional[str] = None,
    ) -> RuntimeV2MemoryQueryResult:
        lane = self._runtime_repository.get_lane(lane_id)
        if lane.conversation_id != conversation_id:
            raise InvalidStateError("Lane does not belong to conversation")
        workspace_id = self._chat_repository.get_conversation(
            conversation_id
        ).workspace_id
        visible_lane_ids = self._ancestor_lane_ids(lane_id)
        records = self._memory_repository.list_visible_memories(
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            lane_id=lane_id,
            run_id=run_id,
            ancestor_lane_ids=visible_lane_ids,
        )
        inherited_branch_cutoff = lane.created_at
        visible_records = tuple(
            memory
            for memory in records
            if memory.scope is not MemoryScope.BRANCH
            or memory.lane_id == lane_id
            or memory.created_at <= inherited_branch_cutoff
        )
        return RuntimeV2MemoryQueryResult(
            records=visible_records,
            visible_lane_ids=visible_lane_ids,
            workspace_id=workspace_id,
        )

    def context_messages(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        run_id: str,
    ) -> tuple[ProviderMessage, ...]:
        result = self.query(
            conversation_id=conversation_id,
            lane_id=lane_id,
            run_id=run_id,
        )
        if not result.records:
            return ()
        lines: list[str] = []
        total_chars = 0
        for memory in result.records[:_MAX_CONTEXT_MEMORIES]:
            line = f"[{memory.scope.value}] {memory.content}"
            if total_chars + len(line) > _MAX_CONTEXT_CHARS:
                break
            lines.append(line)
            total_chars += len(line)
        if not lines:
            return ()
        content = "<runtime-memory>\n" + "\n".join(lines) + "\n</runtime-memory>"
        return (ProviderMessage(role="system", content=content),)

    def create_lane_memory(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        kind: str,
        content: str,
        source_entry_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> RuntimeV2MemoryRecord:
        normalized_kind = self._validate_kind(kind)
        lane = self._runtime_repository.get_lane(lane_id)
        if lane.conversation_id != conversation_id:
            raise InvalidStateError("Lane does not belong to conversation")
        if lane.is_archived:
            raise ConflictError("Archived lanes cannot create memories")
        scope = (
            MemoryScope.TEMPORARY
            if lane.kind is LaneKind.TEMPORARY
            else MemoryScope.BRANCH
        )
        return self._memory_repository.create_memory(
            scope=scope,
            kind=normalized_kind,
            content=content,
            conversation_id=conversation_id,
            lane_id=lane_id,
            source_entry_id=source_entry_id,
            expires_at=expires_at,
        )

    def create_run_memory(
        self,
        *,
        conversation_id: str,
        run_id: str,
        kind: str,
        content: str,
        source_entry_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> RuntimeV2MemoryRecord:
        normalized_kind = self._validate_kind(kind)
        run = self._runtime_repository.get_run(run_id)
        if run.conversation_id != conversation_id:
            raise InvalidStateError("Run does not belong to conversation")
        return self._memory_repository.create_memory(
            scope=MemoryScope.RUN_SCRATCH,
            kind=normalized_kind,
            content=content,
            conversation_id=conversation_id,
            lane_id=run.lane_id,
            run_id=run_id,
            source_entry_id=source_entry_id,
            expires_at=expires_at,
        )

    def create_promotion(
        self,
        *,
        memory_id: str,
        target_scope: MemoryScope,
        target_lane_id: Optional[str] = None,
    ) -> RuntimeV2MemoryPromotion:
        memory = self._memory_repository.get_memory(memory_id)
        target_workspace_id = None
        if target_scope is MemoryScope.WORKSPACE:
            target_workspace_id = self._chat_repository.get_conversation(
                memory.conversation_id
            ).workspace_id
        if target_scope is MemoryScope.BRANCH and target_lane_id is None:
            target_lane_id = self._nearest_persistent_lane_id(memory.lane_id)
        return self._memory_repository.create_promotion(
            source_memory_id=memory_id,
            target_scope=target_scope,
            target_workspace_id=target_workspace_id,
            target_lane_id=target_lane_id,
        )

    def resolve_promotion(
        self,
        promotion_id: str,
        *,
        accept: bool,
    ) -> tuple[RuntimeV2MemoryPromotion, Optional[RuntimeV2MemoryRecord]]:
        return self._memory_repository.resolve_promotion(
            promotion_id,
            accept=accept,
        )

    def list_promotions(
        self,
        *,
        conversation_id: Optional[str] = None,
        source_memory_id: Optional[str] = None,
        include_resolved: bool = False,
    ) -> tuple[RuntimeV2MemoryPromotion, ...]:
        return self._memory_repository.list_promotions(
            source_memory_id=source_memory_id,
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )

    def _ancestor_lane_ids(self, lane_id: str) -> tuple[str, ...]:
        ancestors: list[str] = []
        current_id: Optional[str] = lane_id
        visited: set[str] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            ancestors.append(current_id)
            lane = self._runtime_repository.get_lane(current_id)
            source_lane_id = lane.source_lane_id
            current_id = source_lane_id
        return tuple(ancestors)

    def _nearest_persistent_lane_id(
        self,
        lane_id: Optional[str],
    ) -> str:
        if lane_id is None:
            raise InvalidStateError("Branch memory promotion requires a lane")
        for candidate_id in self._ancestor_lane_ids(lane_id):
            candidate = self._runtime_repository.get_lane(candidate_id)
            if candidate.kind in {
                LaneKind.MAIN,
                LaneKind.PERSISTENT_BRANCH,
            }:
                return candidate.id
        raise InvalidStateError("No persistent lane is available for promotion")

    @staticmethod
    def _validate_kind(kind: str) -> str:
        normalized = kind.strip()
        if normalized not in _MEMORY_KINDS:
            raise ConflictError("Memory kind must be preference or fact")
        return normalized
