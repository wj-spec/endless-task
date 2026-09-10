"""Runtime v2 记录 → JSON 投影（从 app.py 搬出，供 v2 路由族共用）。

行为零改动：字段、顺序、兜底值与搬家前一致。
"""

from __future__ import annotations

from endless_task.runtime_v2 import LaneKind, LaneRecord, ProductRuntimeEventRecord, RunRecord, RuntimeV2MemoryPromotion, RuntimeV2MemoryRecord, product_event_json
import json
from typing import Optional

def runtime_v2_product_sse(event: ProductRuntimeEventRecord) -> str:
    payload = json.dumps(
        product_event_json(event),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_seq}\nevent: {event.event_type}\ndata: {payload}\n\n"


def runtime_v2_lane_json(
    lane: LaneRecord,
    *,
    active_lane_id: Optional[str] = None,
) -> dict[str, object]:
    source_lane_id = lane.source_lane_id
    title = lane.display_name or lane.summary
    return {
        "id": lane.id,
        "conversationId": lane.conversation_id,
        "kind": lane.kind.value,
        "status": lane.status.value,
        "archived": lane.is_archived,
        "archivedAt": lane.archived_at,
        "displayName": lane.display_name,
        "summary": lane.summary,
        "title": title,
        "baseEntryExcerpt": lane.summary,
        "baseEntryId": lane.base_entry_id,
        "leafEntryId": lane.leaf_entry_id,
        "createdFromEntryId": lane.created_from_entry_id,
        "createdAt": lane.created_at,
        "sourceLaneId": source_lane_id,
        "isMain": lane.id == active_lane_id or (
            active_lane_id is None and lane.kind is LaneKind.MAIN
        ),
    }


def runtime_v2_run_variant_json(run: RunRecord) -> dict[str, object]:
    return {
        "runId": run.id,
        "conversationId": run.conversation_id,
        "laneId": run.lane_id,
        "triggerEntryId": run.trigger_entry_id,
        "siblingGroupId": run.sibling_group_id,
        "assistantEntryId": run.assistant_entry_id,
        "status": run.status.value,
        "isActiveVariant": run.is_active_variant,
        "createdAt": run.created_at,
        "finishedAt": run.finished_at,
    }


def runtime_v2_memory_json(memory: RuntimeV2MemoryRecord) -> dict[str, object]:
    return {
        "id": memory.id,
        "scope": memory.scope.value,
        "kind": memory.kind,
        "content": memory.content,
        "status": memory.status,
        "conversationId": memory.conversation_id,
        "workspaceId": memory.workspace_id,
        "laneId": memory.lane_id,
        "runId": memory.run_id,
        "sourceMemoryId": memory.source_memory_id,
        "sourceEntryId": memory.source_entry_id,
        "createdAt": memory.created_at,
        "updatedAt": memory.updated_at,
    }


def runtime_v2_memory_promotion_json(
    promotion: RuntimeV2MemoryPromotion,
) -> dict[str, object]:
    return {
        "id": promotion.id,
        "memoryId": promotion.source_memory_id,
        "targetScope": promotion.target_scope.value,
        "targetWorkspaceId": promotion.target_workspace_id,
        "targetLaneId": promotion.target_lane_id,
        "status": promotion.status.value,
        "resolvedMemoryId": promotion.resolved_memory_id,
        "conflictMemoryId": promotion.conflict_memory_id,
        "createdAt": promotion.created_at,
        "updatedAt": promotion.updated_at,
        "resolvedAt": promotion.resolved_at,
    }
