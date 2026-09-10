"""`RuntimeV2SessionGateway` 的泳道族：分支、泳道记忆、临时会话。

从 `gateway.py` 拆出（行为零改动）；只调用底座 `_GatewayBase` 的校验/查找方法。
"""

from __future__ import annotations

from typing import Optional

from ..domain import (
    LaneKind,
    LaneRecord,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
)
from endless_task.domain.repositories import ConflictError
from ..lane import RuntimeV2LaneCreationResult
from .base import _GatewayBase


class _GatewayLanesMixin:
    async def create_lane_branch(
        self,
        *,
        conversation_id: str,
        source_lane_id: str,
        base_entry_id: str,
        kind: LaneKind = LaneKind.PERSISTENT_BRANCH,
        display_name: Optional[str] = None,
    ) -> RuntimeV2LaneCreationResult:
        self.validate_session(conversation_id)
        if kind is not LaneKind.PERSISTENT_BRANCH:
            raise ConflictError("Temporary conversations are created with the temporary conversation API")
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            return self._lane_service.create_branch(
                conversation_id=conversation_id,
                source_lane_id=source_lane_id,
                base_entry_id=base_entry_id,
                display_name=display_name,
            )

    def list_lanes(
        self,
        conversation_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[LaneRecord, ...]:
        self.validate_session(conversation_id)
        return self._repository.list_lanes(
            conversation_id,
            include_archived=include_archived,
        )

    def list_memories(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        run_id: Optional[str] = None,
    ) -> tuple[RuntimeV2MemoryRecord, ...]:
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.query(
            conversation_id=conversation_id,
            lane_id=lane_id,
            run_id=run_id,
        ).records

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
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.create_lane_memory(
            conversation_id=conversation_id,
            lane_id=lane_id,
            kind=kind,
            content=content,
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
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.create_run_memory(
            conversation_id=conversation_id,
            run_id=run_id,
            kind=kind,
            content=content,
            source_entry_id=source_entry_id,
            expires_at=expires_at,
        )

    def create_memory_promotion(
        self,
        *,
        memory_id: str,
        target_scope: MemoryScope,
        target_lane_id: Optional[str] = None,
    ) -> RuntimeV2MemoryPromotion:
        self._require_memory_service()
        memory = self._memory_repository.get_memory(memory_id)
        self.validate_session(memory.conversation_id)
        return self._memory_service.create_promotion(
            memory_id=memory_id,
            target_scope=target_scope,
            target_lane_id=target_lane_id,
        )

    def list_memory_promotions(
        self,
        *,
        conversation_id: Optional[str] = None,
        source_memory_id: Optional[str] = None,
        include_resolved: bool = False,
    ) -> tuple[RuntimeV2MemoryPromotion, ...]:
        if conversation_id is not None:
            self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.list_promotions(
            conversation_id=conversation_id,
            source_memory_id=source_memory_id,
            include_resolved=include_resolved,
        )

    def resolve_memory_promotion(
        self,
        promotion_id: str,
        *,
        accept: bool,
    ) -> tuple[RuntimeV2MemoryPromotion, Optional[RuntimeV2MemoryRecord]]:
        self._require_memory_service()
        promotion = self._memory_repository.get_promotion(promotion_id)
        memory = self._memory_repository.get_memory(promotion.source_memory_id)
        self.validate_session(memory.conversation_id)
        return self._memory_service.resolve_promotion(
            promotion_id,
            accept=accept,
        )

    async def promote_lane(
        self,
        *,
        conversation_id: str,
        target_lane_id: str,
    ) -> LanePromotionRecord:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            return self._lane_service.promote_lane(
                conversation_id=conversation_id,
                target_lane_id=target_lane_id,
            )

    async def rename_lane(
        self,
        lane_id: str,
        display_name: Optional[str],
    ) -> LaneRecord:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.rename_lane(lane_id, display_name)

    async def archive_lane(
        self,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.archive_lane(
                conversation_id=lane.conversation_id,
                lane_id=lane_id,
            )

    async def restore_lane(
        self,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.restore_lane(
                conversation_id=lane.conversation_id,
                lane_id=lane_id,
            )

    async def create_temporary_conversation(
        self,
        *,
        source_conversation_id: str,
        source_lane_id: str,
        source_leaf_entry_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[str, LaneRecord]:
        self.validate_session(source_conversation_id)
        pointer = self._repository.get_conversation_pointer(source_conversation_id)
        if pointer is None:
            raise ConflictError("Conversation has no v2 main lane")
        if source_lane_id != pointer.active_lane_id:
            raise ConflictError(
                "Temporary conversations must snapshot the current main lane"
            )
        main_lane = self._repository.get_lane(pointer.active_lane_id)
        if source_leaf_entry_id not in (None, main_lane.leaf_entry_id):
            raise ConflictError(
                "Temporary conversations must snapshot the complete main lane path"
            )
        source_leaf_entry_id = main_lane.leaf_entry_id
        async with self._lock:
            self._require_no_active_runs(source_conversation_id)
            conversation_id, lane, _provenance = (
                self._repository.create_temporary_conversation_from_lane(
                    source_conversation_id=source_conversation_id,
                    source_lane_id=source_lane_id,
                    source_leaf_entry_id=source_leaf_entry_id,
                    title=title,
                )
            )
            return conversation_id, lane

    async def promote_temporary_conversation(
        self,
        conversation_id: str,
    ) -> None:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            self._repository.promote_temporary_conversation(conversation_id)

    async def delete_temporary_conversation(self, conversation_id: str) -> None:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            self._repository.delete_temporary_conversation(conversation_id)
