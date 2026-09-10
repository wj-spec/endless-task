"""v2 lane 路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/api/v2/conversations/{id}/lanes`（列表/创建）、
`/api/v2/lanes/{id}/promote|archive|restore`。
"""

from __future__ import annotations

from fastapi import FastAPI

from ..container import AppContainer
from ..errors import ApiRequestError
from ..runtime_v2_support import runtime_v2_lane_json
from ..schemas.v2_lanes import RuntimeV2RenameLaneBody


def register_v2_lanes_routes(app: FastAPI, container: AppContainer) -> None:
    @app.post("/api/v2/lanes/{lane_id}/promote")
    async def promote_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lane = container.runtime_v2_repository.get_lane(lane_id)
        result = await container.runtime_v2_gateway.promote_lane(
            conversation_id=lane.conversation_id,
            target_lane_id=lane_id,
        )
        return {
            "lane": runtime_v2_lane_json(result.promoted_lane),
            "previousMainLane": runtime_v2_lane_json(result.previous_main_lane),
            "activeLaneId": result.pointer.active_lane_id,
        }


    @app.patch("/api/v2/lanes/{lane_id}")
    async def rename_runtime_v2_lane(
        lane_id: str,
        body: RuntimeV2RenameLaneBody,
    ) -> dict[str, object]:
        lane = await container.runtime_v2_gateway.rename_lane(
            lane_id,
            body.displayName,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            lane.conversation_id
        )
        return {
            "lane": runtime_v2_lane_json(
                lane,
                active_lane_id=pointer.active_lane_id if pointer is not None else None,
            )
        }


    @app.post("/api/v2/lanes/{lane_id}/archive")
    async def archive_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lanes = await container.runtime_v2_gateway.archive_lane(lane_id)
        active_lane_id = None
        if lanes:
            pointer = container.runtime_v2_repository.get_conversation_pointer(
                lanes[0].conversation_id
            )
            active_lane_id = pointer.active_lane_id if pointer is not None else None
        return {
            "items": tuple(
                runtime_v2_lane_json(lane, active_lane_id=active_lane_id)
                for lane in lanes
            )
        }


    @app.post("/api/v2/lanes/{lane_id}/restore")
    async def restore_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lanes = await container.runtime_v2_gateway.restore_lane(lane_id)
        active_lane_id = None
        if lanes:
            pointer = container.runtime_v2_repository.get_conversation_pointer(
                lanes[0].conversation_id
            )
            active_lane_id = pointer.active_lane_id if pointer is not None else None
        return {
            "items": tuple(
                runtime_v2_lane_json(lane, active_lane_id=active_lane_id)
                for lane in lanes
            )
        }
