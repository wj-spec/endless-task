"""Hub 事件投影（从 app.py 搬出，供 v2 路由族与提案流程共用）。

行为零改动：字段、顺序、兜底值与搬家前一致。
"""

from __future__ import annotations

from .serialization import hub_event_json
import json

def hub_event_sse(event) -> str:
    payload = json.dumps(
        hub_event_json(event),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_seq}\nevent: hub.{event.event_type}\ndata: {payload}\n\n"


def hub_append_proposal_resolved(
    container,
    *,
    kind: str,
    proposal,
    decision: str,
) -> None:
    """P1-2 resolve 写点：提案 accept/reject 后推 hub 事件（pending 集合变化）。"""
    container.hub_event_repository.append(
        "proposal.resolved",
        conversation_id=proposal.conversation_id,
        data={
            "kind": kind,
            "proposalId": proposal.id,
            "conversationId": proposal.conversation_id,
            "decision": decision,
        },
    )


def hub_append_memory_consolidated(
    container,
    *,
    proposal,
    memory,
    source_memory_ids,
) -> None:
    """B2：巩固落地后推 hub 事件（记忆面板/审计轨迹据此刷新）。"""
    container.hub_event_repository.append(
        "memory.consolidated",
        conversation_id=proposal.conversation_id,
        data={
            "proposalId": proposal.id,
            "conversationId": proposal.conversation_id,
            "insightMemoryId": memory.id,
            "sourceMemoryIds": list(source_memory_ids),
            "sourceCount": len(source_memory_ids),
        },
    )
