"""v2 记忆路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/api/v2/memories/{id}/promotions`、`/api/v2/memory-promotions/{id}/resolve`。
"""

from __future__ import annotations

from fastapi import FastAPI

from endless_task.runtime_v2 import MemoryScope

from ..container import AppContainer
from ..errors import ApiRequestError
from ..runtime_v2_support import (
    runtime_v2_memory_json,
    runtime_v2_memory_promotion_json,
)
from ..schemas.v2_memory import (
    RuntimeV2MemoryPromotionBody,
    RuntimeV2MemoryPromotionResolveBody,
)


def register_v2_memory_routes(app: FastAPI, container: AppContainer) -> None:
    @app.post("/api/v2/memories/{memory_id}/promotions", status_code=201)
    async def create_runtime_v2_memory_promotion(
        memory_id: str,
        body: RuntimeV2MemoryPromotionBody,
    ) -> dict[str, object]:
        promotion = container.runtime_v2_gateway.create_memory_promotion(
            memory_id=memory_id,
            target_scope=MemoryScope(body.targetScope),
            target_lane_id=body.targetLaneId,
        )
        return {"promotion": runtime_v2_memory_promotion_json(promotion)}


    @app.post("/api/v2/memory-promotions/{promotion_id}/resolve")
    async def resolve_runtime_v2_memory_promotion(
        promotion_id: str,
        body: RuntimeV2MemoryPromotionResolveBody,
    ) -> dict[str, object]:
        promotion, memory = container.runtime_v2_gateway.resolve_memory_promotion(
            promotion_id,
            accept=body.decision == "accept",
        )
        return {
            "promotion": runtime_v2_memory_promotion_json(promotion),
            "memory": (
                runtime_v2_memory_json(memory)
                if memory is not None
                else None
            ),
        }
