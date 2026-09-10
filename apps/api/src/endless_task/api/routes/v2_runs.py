"""v2 运行路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/api/v2/runs/{id}/variants|resend|regenerate|select|usage-cost|audit-trail|
spans|memories|recovery|steer|cancel`。
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Header

from endless_task.runtime_v2 import MemoryScope

from ..container import AppContainer
from ..errors import ApiRequestError
from ..runtime_v2_support import runtime_v2_run_variant_json
from ..schemas.v2_runs import (
    ResendRuntimeV2RunBody,
    RuntimeV2RecoveryBody,
    RuntimeV2RunMemoryBody,
    RuntimeV2SteerBody,
)
from ..v2_conversation_support import resolve_runtime_v2_conversation


def register_v2_runs_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/api/v2/runs/{run_id}/variants")
    async def list_runtime_v2_run_variants(run_id: str) -> dict[str, object]:
        variants = container.runtime_v2_gateway.list_run_variants(run_id)
        return {
            "runId": run_id,
            "siblingGroupId": variants[0].sibling_group_id if variants else None,
            "items": tuple(runtime_v2_run_variant_json(variant) for variant in variants),
        }


    @app.post("/api/v2/runs/{run_id}/resend", status_code=202)
    async def resend_runtime_v2_run(
        run_id: str, body: ResendRuntimeV2RunBody
    ) -> dict[str, object]:
        result = await container.runtime_v2_gateway.resend_run(run_id, body.content)
        return {
            "oldRunId": result.old_run_id,
            "newRunId": result.new_run_id,
            "laneId": result.lane_id,
            "triggerEntryId": result.trigger_entry_id,
            "siblingGroupId": result.sibling_group_id,
            "eventsUrl": (
                f"/api/v2/conversations/"
                f"{container.runtime_v2_repository.get_run(run_id).conversation_id}/events"
            ),
        }


    @app.post("/api/v2/runs/{run_id}/regenerate", status_code=202)
    async def regenerate_runtime_v2_run(run_id: str) -> dict[str, object]:
        result = await container.runtime_v2_gateway.regenerate_run(run_id)
        return {
            "oldRunId": result.old_run_id,
            "newRunId": result.new_run_id,
            "laneId": result.lane_id,
            "triggerEntryId": result.trigger_entry_id,
            "siblingGroupId": result.sibling_group_id,
            "eventsUrl": (
                f"/api/v2/conversations/"
                f"{container.runtime_v2_repository.get_run(run_id).conversation_id}/events"
            ),
        }


    @app.post("/api/v2/runs/{run_id}/select")
    async def select_runtime_v2_run_variant(run_id: str) -> dict[str, object]:
        selected = await container.runtime_v2_gateway.select_run_variant(run_id)
        return {
            "runId": selected.id,
            "assistantEntryId": selected.assistant_entry_id,
            "isActiveVariant": selected.is_active_variant,
        }


    @app.get("/api/v2/runs/{run_id}/usage-cost")
    async def get_runtime_v2_run_usage_cost(run_id: str) -> dict[str, object]:
        # P1-1: diagnostic cost/usage for one run (trace ledger rows).
        container.runtime_v2_repository.get_run(run_id)  # 404 if unknown
        ledger = container.runtime_v2_span_recorder
        rows: list[dict[str, object]] = []
        totals = {
            "inputTokens": 0,
            "outputTokens": 0,
            "requestCount": 0,
            "costUsd": 0.0,
        }
        if ledger is not None and hasattr(ledger, "usage_for_run"):
            for usage in ledger.usage_for_run(run_id):
                rows.append(
                    {
                        "provider": usage.provider,
                        "model": usage.model,
                        "inputTokens": usage.input_tokens,
                        "outputTokens": usage.output_tokens,
                        "requestCount": usage.request_count,
                        "costUsd": usage.cost_usd,
                    }
                )
                totals["inputTokens"] += usage.input_tokens or 0
                totals["outputTokens"] += usage.output_tokens or 0
                totals["requestCount"] += usage.request_count or 0
                totals["costUsd"] += usage.cost_usd or 0.0
        return {"runId": run_id, "rows": rows, "totals": totals}


    @app.get("/api/v2/runs/{run_id}/audit-trail")
    async def get_runtime_v2_run_audit_trail(run_id: str) -> dict[str, object]:
        """A8：把一次运行的事实翻译成"为什么"的可审阅轨迹。"""
        run = container.runtime_v2_repository.get_run(run_id)  # 404 if unknown
        facts: list[AuditFact] = []
        for event in container.runtime_v2_repository.list_runtime_events(run_id):
            fact = audit_fact_from_event(container, event)
            if fact is not None:
                facts.append(fact)
        for entry in container.undo_journal_repository.list_for_conversation(
            run.conversation_id, limit=50
        ):
            if entry.run_id != run_id:
                continue
            facts.append(
                AuditFact(
                    fact_id=entry.id,
                    kind="undo",
                    occurred_at=entry.undone_at or entry.created_at,
                    title="撤销了这次改动",
                    summary=entry.description,
                    payload={
                        "target": entry.target,
                        "kind": entry.kind,
                        "refs": {"undoEntryId": entry.id},
                    },
                )
            )
        entries = build_audit_trail(facts)
        return {
            "runId": run_id,
            "conversationId": run.conversation_id,
            "items": [entry.as_json() for entry in entries],
            "counts": {
                severity: sum(1 for item in entries if item.severity == severity)
                for severity in ("info", "warning", "critical")
            },
        }


    @app.get("/api/v2/runs/{run_id}/spans")
    async def get_runtime_v2_run_spans(run_id: str) -> dict[str, object]:
        """S-P1-3b：run 的 span 只读列表（执行时间线事实源）。"""
        container.runtime_v2_repository.get_run(run_id)  # 404 if unknown
        recorder = container.runtime_v2_span_recorder
        if recorder is None or not hasattr(recorder, "spans_for_run"):
            return {"runId": run_id, "available": False, "spans": []}
        spans = [
            {
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id,
                "kind": span.kind,
                "name": span.name,
                "status": span.status,
                "startedAt": span.started_at,
                "endedAt": span.ended_at,
                "durationMs": span.monotonic_duration_ms,
                "diagnosticCode": span.diagnostic_code,
                "diagnosticMessage": span.diagnostic_message,
            }
            for span in recorder.spans_for_run(run_id)
        ]
        return {"runId": run_id, "available": True, "spans": spans}


    @app.post("/api/v2/runs/{run_id}/memories", status_code=201)
    async def create_runtime_v2_run_memory(
        run_id: str,
        body: RuntimeV2RunMemoryBody,
    ) -> dict[str, object]:
        run = container.runtime_v2_repository.get_run(run_id)
        memory = container.runtime_v2_gateway.create_run_memory(
            conversation_id=run.conversation_id,
            run_id=run_id,
            kind=body.kind,
            content=body.content,
            source_entry_id=body.sourceEntryId,
            expires_at=body.expiresAt,
        )
        return {"memory": runtime_v2_memory_json(memory)}


    @app.post("/api/v2/runs/{run_id}/recovery")
    async def resolve_runtime_v2_recovery(
        run_id: str,
        body: RuntimeV2RecoveryBody,
    ) -> dict[str, object]:
        result = await container.runtime_v2_gateway.resolve_recovery(
            run_id,
            retry=body.action == "retry",
        )
        return {
            "runId": result.run_id,
            "action": result.action,
            "laneId": result.lane_id,
            "newRunId": result.new_run_id,
        }


    @app.post("/api/v2/runs/{run_id}/steer")
    async def steer_runtime_v2_run(
        run_id: str,
        body: RuntimeV2SteerBody,
    ) -> dict[str, object]:
        accepted = await container.runtime_v2_gateway.steer(
            run_id,
            body.content,
        )
        return {"runId": run_id, "accepted": accepted}


    @app.post("/api/v2/runs/{run_id}/cancel")
    async def cancel_runtime_v2_run(run_id: str) -> dict[str, object]:
        accepted = await container.runtime_v2_gateway.cancel(run_id)
        return {"runId": run_id, "accepted": accepted}
