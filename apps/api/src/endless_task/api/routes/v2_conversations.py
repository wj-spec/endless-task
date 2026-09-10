"""v2 会话路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/api/v2/conversations/{id}` 的发言（POST messages）、快照、lane 列表/创建、
SSE 事件流，以及 `/api/v2/temporary-conversations/{id}/promote`。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Header, Query
from fastapi.responses import StreamingResponse

from endless_task.domain.repositories import ConflictError
from endless_task.runtime_v2 import LaneKind, RunStatus

from ..container import AppContainer
from ..errors import ApiRequestError
from ..runtime_v2_support import (
    runtime_v2_lane_json,
    runtime_v2_memory_json,
    runtime_v2_memory_promotion_json,
    runtime_v2_product_sse,
)
from ..schemas.v2_conversations import (
    RuntimeV2CreateLaneBody,
    RuntimeV2CreateTemporaryConversationBody,
    RuntimeV2MemoryBody,
    RuntimeV2MessageBody,
)
from ..serialization import conversation_json
from ..skill_requests import resolve_skill_requests
from ..v2_conversation_support import (
    resolve_runtime_v2_conversation,
    title_conversation_from_first_message,
)


def register_v2_conversations_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/api/v2/conversations/{conversation_id}/lanes")
    async def list_runtime_v2_lanes(
        conversation_id: str,
        includeArchived: bool = False,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        lanes = container.runtime_v2_gateway.list_lanes(
            target_conversation_id,
            include_archived=includeArchived,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        main_lane_id = pointer.active_lane_id if pointer is not None else None
        running_runs = container.runtime_v2_repository.list_runs(
            conversation_id=target_conversation_id,
            statuses=(
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.COMPACTING,
                RunStatus.CANCELLING,
            ),
        )
        running_run = running_runs[-1] if running_runs else None
        return {
            "conversationId": target_conversation_id,
            "activeLaneId": main_lane_id,
            "mainLaneId": main_lane_id,
            "runningLaneId": running_run.lane_id if running_run is not None else None,
            "runningRunId": running_run.id if running_run is not None else None,
            "items": tuple(
                runtime_v2_lane_json(lane, active_lane_id=main_lane_id)
                for lane in lanes
            ),
        }


    @app.post("/api/v2/conversations/{conversation_id}/lanes", status_code=201)
    async def create_runtime_v2_lane(
        conversation_id: str,
        body: RuntimeV2CreateLaneBody,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        source_lane_id = body.sourceLaneId or pointer.active_lane_id
        base_entry_id = body.baseEntryId
        if base_entry_id is None:
            source_lane = container.runtime_v2_repository.get_lane(source_lane_id)
            base_entry_id = source_lane.leaf_entry_id
            if base_entry_id is None:
                raise ConflictError("Source lane has no base entry")
        result = await container.runtime_v2_gateway.create_lane_branch(
            conversation_id=target_conversation_id,
            source_lane_id=source_lane_id,
            base_entry_id=base_entry_id,
            kind=LaneKind.PERSISTENT_BRANCH,
            display_name=body.displayName,
        )
        return {
            "lane": runtime_v2_lane_json(result.lane),
            "sourceLane": runtime_v2_lane_json(result.source_lane),
            "baseEntryId": result.base_entry_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
        }


    @app.post(
        "/api/v2/conversations/{conversation_id}/temporary-conversations",
        status_code=201,
    )
    async def create_runtime_v2_temporary_conversation(
        conversation_id: str,
        body: RuntimeV2CreateTemporaryConversationBody,
    ) -> dict[str, object]:
        source_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            source_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 main lane")
        main_lane = container.runtime_v2_repository.get_lane(pointer.active_lane_id)
        if main_lane.leaf_entry_id is None:
            raise ConflictError("Conversation main lane has no context to copy")
        temporary_conversation_id, lane = (
            await container.runtime_v2_gateway.create_temporary_conversation(
                source_conversation_id=source_conversation_id,
                source_lane_id=main_lane.id,
                source_leaf_entry_id=main_lane.leaf_entry_id,
                title=body.title,
            )
        )
        conversation = container.chat_repository.get_conversation(
            temporary_conversation_id
        )
        return {
            "conversation": conversation_json(conversation),
            "lane": runtime_v2_lane_json(lane, active_lane_id=lane.id),
        }


    @app.get("/api/v2/conversations/{conversation_id}/memories")
    async def list_runtime_v2_memories(
        conversation_id: str,
        lane_id: str = Query(...),
        run_id: Optional[str] = Query(None),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        memories = container.runtime_v2_gateway.list_memories(
            conversation_id=target_conversation_id,
            lane_id=lane_id,
            run_id=run_id,
        )
        return {
            "conversationId": target_conversation_id,
            "laneId": lane_id,
            "items": tuple(runtime_v2_memory_json(memory) for memory in memories),
        }


    @app.post("/api/v2/conversations/{conversation_id}/memories", status_code=201)
    async def create_runtime_v2_memory(
        conversation_id: str,
        body: RuntimeV2MemoryBody,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        lane_id = body.laneId or pointer.active_lane_id
        memory = container.runtime_v2_gateway.create_lane_memory(
            conversation_id=target_conversation_id,
            lane_id=lane_id,
            kind=body.kind,
            content=body.content,
            source_entry_id=body.sourceEntryId,
            expires_at=body.expiresAt,
        )
        return {"memory": runtime_v2_memory_json(memory)}


    @app.get("/api/v2/conversations/{conversation_id}/memory-promotions")
    async def list_runtime_v2_memory_promotions(
        conversation_id: str,
        include_resolved: bool = Query(False),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        promotions = container.runtime_v2_gateway.list_memory_promotions(
            conversation_id=target_conversation_id,
            include_resolved=include_resolved,
        )
        return {
            "conversationId": target_conversation_id,
            "items": tuple(
                runtime_v2_memory_promotion_json(promotion)
                for promotion in promotions
            ),
        }


    @app.post("/api/v2/conversations/{conversation_id}/messages", status_code=202)
    async def create_runtime_v2_message(
        conversation_id: str,
        body: RuntimeV2MessageBody,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        if len(body.content) > container.settings.max_message_characters:
            raise ApiRequestError(
                "message_too_large",
                "消息内容超过本地配置允许的长度。",
                status_code=413,
            )
        _skill_messages, skill_notices = resolve_skill_requests(
            container.skill_service,
            container.workspace_resolver,
            target_conversation_id,
            body.content,
            include_bodies=False,
        )
        title_conversation_from_first_message(
            container, target_conversation_id, body.content
        )
        handle = await container.runtime_v2_gateway.send(
            target_conversation_id,
            body.content,
            lane_id=body.laneId,
            client_request_id=idempotency_key,
        )
        return {
            "conversationId": handle.conversation_id,
            "laneId": handle.lane_id,
            "runId": handle.run_id,
            "userMessageId": handle.user_message_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
            # S1：显式技能调用结果（ok / disabled / not_user_invocable / …）
            "requestedSkills": skill_notices,
        }


    @app.get("/api/v2/conversations/{conversation_id}/snapshot")
    async def get_runtime_v2_snapshot(
        conversation_id: str,
        lane_id: Optional[str] = Query(default=None),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        return container.runtime_v2_gateway.snapshot(
            target_conversation_id,
            lane_id=lane_id,
        )


    @app.get("/api/v2/conversations/{conversation_id}/recovery")
    async def get_runtime_v2_recovery(
        conversation_id: str,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        reports = container.runtime_v2_gateway.recovery_reports(
            target_conversation_id
        )
        return {
            "conversationId": target_conversation_id,
            "interruptedRuns": tuple(
                {
                    "runId": report.record.id,
                    "status": report.record.status.value,
                    "classification": report.classification.value,
                    "action": report.action.value,
                    "findings": tuple(
                        {
                            "reason": finding.reason.value,
                            "message": finding.message,
                            "modelTurnId": finding.model_turn_id,
                            "toolExecutionId": finding.tool_execution_id,
                        }
                        for finding in report.findings
                    ),
                }
                for report in reports
            ),
        }


    @app.get("/api/v2/conversations/{conversation_id}/events")
    async def get_runtime_v2_events(
        conversation_id: str,
        after_seq: int = Query(0, ge=0),
        lane_id: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        initial_events = container.runtime_v2_gateway.project_events(
            target_conversation_id
        )
        latest_event_seq = initial_events[-1].event_seq if initial_events else 0
        if after_seq > latest_event_seq:
            raise ApiRequestError(
                "invalid_after_seq",
                "after_seq 超过当前会话事件游标。",
            )

        async def stream() -> AsyncIterator[str]:
            snapshot = container.runtime_v2_gateway.snapshot(
                target_conversation_id,
                lane_id=lane_id,
            )
            snapshot_event_seq = int(snapshot["lastEventSeq"])
            snapshot_payload = json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield (
                f"id: snapshot-{conversation_id}-{snapshot_event_seq}\n"
                "event: conversation.snapshot_ready\n"
                f"data: {snapshot_payload}\n\n"
            )
            cursor = snapshot_event_seq
            last_heartbeat = asyncio.get_running_loop().time()
            poll_interval = min(0.05, container.settings.heartbeat_seconds)
            while True:
                events = container.runtime_v2_gateway.project_events(
                    target_conversation_id
                )
                if lane_id is not None:
                    events = tuple(
                        event for event in events if event.lane_id == lane_id
                    )
                emitted = False
                for event in events:
                    if event.event_seq <= cursor:
                        continue
                    cursor = event.event_seq
                    emitted = True
                    yield runtime_v2_product_sse(event)
                if not container.runtime_v2_gateway.has_active_run(
                    target_conversation_id
                ):
                    final_events = container.runtime_v2_gateway.project_events(
                        target_conversation_id
                    )
                    if lane_id is not None:
                        final_events = tuple(
                            event
                            for event in final_events
                            if event.lane_id == lane_id
                        )
                    for event in final_events:
                        if event.event_seq <= cursor:
                            continue
                        cursor = event.event_seq
                        yield runtime_v2_product_sse(event)
                    return
                now = asyncio.get_running_loop().time()
                if (
                    not emitted
                    and now - last_heartbeat >= container.settings.heartbeat_seconds
                ):
                    last_heartbeat = now
                    yield ": heartbeat\n\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
