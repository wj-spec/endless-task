from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from endless_task.domain.repositories import ConflictError, InvalidStateError

from .domain import LaneKind, LanePromotionRecord, LaneRecord

if TYPE_CHECKING:
    from endless_task.storage import SqliteRuntimeV2Repository


@dataclass(frozen=True)
class RuntimeV2LaneCreationResult:
    lane: LaneRecord
    source_lane: LaneRecord
    base_entry_id: str


class RuntimeV2LaneService:
    def __init__(self, repository: SqliteRuntimeV2Repository) -> None:
        self._repository = repository

    def list_lanes(
        self,
        conversation_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[LaneRecord, ...]:
        return self._repository.list_lanes(
            conversation_id,
            include_archived=include_archived,
        )

    def create_branch(
        self,
        *,
        conversation_id: str,
        source_lane_id: str,
        base_entry_id: str,
        display_name: str | None = None,
    ) -> RuntimeV2LaneCreationResult:
        source = self._repository.get_lane(source_lane_id)
        if source.conversation_id != conversation_id:
            raise InvalidStateError("Source lane does not belong to conversation")
        if source.kind is LaneKind.TEMPORARY:
            raise ConflictError("Temporary conversations cannot create branch lanes")
        if source.is_archived:
            raise ConflictError("Archived lanes cannot create branch lanes")
        context_entries = self._repository.list_lane_context_entries(source.id)
        context_entry_ids = {entry.id for entry in context_entries}
        if base_entry_id not in context_entry_ids:
            raise InvalidStateError("Base entry is not on the source lane context path")
        base_entry = self._repository.get_entry(base_entry_id)
        base_excerpt = self._entry_excerpt(base_entry)

        lane = self._repository.create_lane(
            conversation_id=conversation_id,
            kind=LaneKind.PERSISTENT_BRANCH,
            base_entry_id=base_entry_id,
            metadata={"sourceLaneId": source.id},
            display_name=display_name,
            summary=base_excerpt,
            source_lane_id=source.id,
            created_from_entry_id=base_entry_id,
            lane_event_type="branch.created",
            lane_event_data={
                "kind": LaneKind.PERSISTENT_BRANCH.value,
                "sourceLaneId": source.id,
                "baseEntryId": base_entry_id,
                "baseEntryExcerpt": base_excerpt,
            },
        )
        return RuntimeV2LaneCreationResult(
            lane=lane,
            source_lane=source,
            base_entry_id=base_entry_id,
        )

    def promote_lane(
        self,
        *,
        conversation_id: str,
        target_lane_id: str,
    ) -> LanePromotionRecord:
        target = self._repository.get_lane(target_lane_id)
        if target.conversation_id != conversation_id:
            raise InvalidStateError("Target lane does not belong to conversation")
        if target.kind is LaneKind.TEMPORARY:
            raise ConflictError("Temporary conversation lanes cannot be promoted as branches")
        if target.is_archived:
            raise ConflictError("Archived lanes cannot be promoted")
        pointer = self._repository.get_conversation_pointer(conversation_id)
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        previous_main = self._repository.get_lane(pointer.active_lane_id)
        result = self._repository.promote_lane(
            conversation_id=conversation_id,
            target_lane_id=target_lane_id,
            lane_event_type="branch.promoted",
            lane_event_data={
                "previousMainLaneId": previous_main.id,
                "promotedFrom": target.kind.value,
            },
        )
        return result

    def rename_lane(self, lane_id: str, display_name: str | None) -> LaneRecord:
        lane = self._repository.get_lane(lane_id)
        pointer = self._repository.get_conversation_pointer(lane.conversation_id)
        is_main = (
            pointer.active_lane_id == lane.id
            if pointer is not None
            else lane.kind is LaneKind.MAIN
        )
        if is_main:
            raise InvalidStateError("Main lane display is derived from the conversation")
        if lane.kind is LaneKind.TEMPORARY:
            raise InvalidStateError("Temporary conversation lanes are renamed via conversation title")
        return self._repository.rename_lane(
            lane_id,
            display_name,
            lane_event_type="branch.renamed",
            lane_event_data={"laneId": lane_id},
        )

    def archive_lane(
        self,
        *,
        conversation_id: str,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        if lane.conversation_id != conversation_id:
            raise InvalidStateError("Lane does not belong to conversation")
        return self._repository.archive_lane_subtree(
            lane_id,
            lane_event_type="branch.archived",
            lane_event_data={"laneId": lane_id},
        )

    def restore_lane(
        self,
        *,
        conversation_id: str,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        if lane.conversation_id != conversation_id:
            raise InvalidStateError("Lane does not belong to conversation")
        return self._repository.restore_lane_subtree(
            lane_id,
            lane_event_type="branch.restored",
            lane_event_data={"laneId": lane_id},
        )

    @staticmethod
    def _entry_excerpt(entry) -> str:
        content = entry.payload.get("content")
        if not isinstance(content, str):
            return ""
        normalized = " ".join(content.split())
        if len(normalized) <= 80:
            return normalized
        return normalized[:79] + "…"
