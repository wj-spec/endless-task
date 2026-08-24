from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from typing import Any, Mapping, Optional

from endless_task.domain.models import (
    Conversation,
    ConversationSnapshot,
    Message,
    ResponseVariant,
    ResponseVariantSnapshot,
    Turn,
    TurnSnapshot,
)
from endless_task.files import UploadedTextFile
from endless_task.runtime.events import RuntimeEvent
from endless_task.tooling import ApprovalRequest


def _camel_key(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {_camel_key(str(key)): _json_value(item) for key, item in value.items()}
    return value


def _dataclass_dict(value: Any) -> dict[str, Any]:
    return {_camel_key(key): _json_value(item) for key, item in asdict(value).items()}


def conversation_json(conversation: Conversation) -> dict[str, Any]:
    return _dataclass_dict(conversation)


def uploaded_text_file_json(file: UploadedTextFile) -> dict[str, Any]:
    return _dataclass_dict(file)


def message_json(message: Message) -> dict[str, Any]:
    return _dataclass_dict(message)


def turn_json(turn: Turn) -> dict[str, Any]:
    return _dataclass_dict(turn)


def response_variant_json(variant: ResponseVariant) -> dict[str, Any]:
    return _dataclass_dict(variant)


def response_variant_snapshot_json(
    snapshot: ResponseVariantSnapshot,
) -> dict[str, Any]:
    return {
        "variant": response_variant_json(snapshot.variant),
        "assistantMessage": message_json(snapshot.assistant_message),
    }


def full_turn_snapshot_json(snapshot: TurnSnapshot) -> dict[str, Any]:
    return {
        "turn": turn_json(snapshot.turn),
        "userMessage": message_json(snapshot.user_message),
        "activeResponseVariantId": snapshot.turn.active_response_variant_id,
        "responseVariants": [
            response_variant_snapshot_json(item) for item in snapshot.response_variants
        ],
    }


def conversation_snapshot_json(
    snapshot: ConversationSnapshot,
    *,
    events_by_turn: Optional[Mapping[str, tuple[RuntimeEvent, ...]]] = None,
) -> dict[str, Any]:
    turns = []
    for item in snapshot.turns:
        payload = full_turn_snapshot_json(item)
        if events_by_turn is not None:
            payload["activities"] = activity_snapshots_json(
                events_by_turn.get(item.turn.id, ()),
                response_variant_id=item.turn.active_response_variant_id,
            )
        turns.append(payload)
    return {
        "conversation": conversation_json(snapshot.conversation),
        "turns": turns,
    }


def runtime_event_json(event: RuntimeEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": event.version,
        "eventId": event.event_id,
        "sequence": event.sequence,
        "type": event.type,
        "conversationId": event.conversation_id,
        "turnId": event.turn_id,
        "occurredAt": event.occurred_at,
        "data": dict(event.data),
    }
    if event.response_variant_id is not None:
        payload["responseVariantId"] = event.response_variant_id
    if event.message_id is not None:
        payload["messageId"] = event.message_id
    return payload


def active_variant(snapshot: TurnSnapshot) -> ResponseVariantSnapshot:
    variant_id = snapshot.turn.active_response_variant_id
    selected = next(
        (item for item in snapshot.response_variants if item.variant.id == variant_id),
        None,
    )
    if selected is None:
        raise RuntimeError("Turn has no active response variant")
    return selected


def response_variant_by_id(
    snapshot: TurnSnapshot,
    variant_id: str,
) -> ResponseVariantSnapshot:
    selected = next(
        (item for item in snapshot.response_variants if item.variant.id == variant_id),
        None,
    )
    if selected is None:
        raise RuntimeError("Response variant does not belong to the turn")
    return selected


def compact_turn_snapshot_json(
    snapshot: TurnSnapshot,
    *,
    events: tuple[RuntimeEvent, ...],
    pending_approval: Optional[ApprovalRequest] = None,
) -> dict[str, Any]:
    selected = active_variant(snapshot)
    failure = next(
        (event for event in reversed(events) if event.type == "turn.failed"),
        None,
    )
    result: dict[str, Any] = {
        "conversationId": snapshot.turn.conversation_id,
        "turnId": snapshot.turn.id,
        "turnStatus": snapshot.turn.status.value,
        "activeResponseVariantId": selected.variant.id,
        "responseVariantStatus": selected.variant.status.value,
        "assistantMessageId": selected.assistant_message.id,
        "content": selected.assistant_message.content,
        "lastSequence": events[-1].sequence if events else 0,
        "activities": activity_snapshots_json(
            events,
            response_variant_id=selected.variant.id,
        ),
    }
    if failure is not None and failure.response_variant_id == selected.variant.id:
        error = failure.data.get("error")
        if isinstance(error, Mapping):
            result["error"] = dict(error)
    if pending_approval is not None:
        result["pendingApproval"] = approval_request_json(pending_approval)
    return result


def approval_request_json(approval: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": approval.id,
        "toolCallId": approval.tool_call_id,
        "summary": approval.summary,
        "reason": approval.reason,
        "status": approval.status.value,
        "createdAt": approval.created_at,
        "resolvedAt": approval.resolved_at,
        "metadata": dict(approval.metadata),
    }


def activity_snapshots_json(
    events: tuple[RuntimeEvent, ...],
    *,
    response_variant_id: Optional[str],
) -> list[dict[str, Any]]:
    activities: dict[str, dict[str, Any]] = {}
    for event in events:
        if not event.type.startswith("activity."):
            continue
        if event.response_variant_id != response_variant_id:
            continue
        activity_id = event.data.get("activityId")
        status = event.data.get("status")
        message = event.data.get("message")
        if not all(isinstance(value, str) for value in (activity_id, status, message)):
            continue
        current = activities.get(activity_id)
        if current is None:
            current = {
                "id": activity_id,
                "status": status,
                "message": message,
                "startedAt": event.occurred_at,
                "updatedAt": event.occurred_at,
            }
            activities[activity_id] = current
        else:
            current.update(
                status=status,
                message=message,
                updatedAt=event.occurred_at,
            )
    return list(activities.values())


def turn_command_json(snapshot: TurnSnapshot) -> dict[str, Any]:
    selected = active_variant(snapshot)
    return {
        "conversationId": snapshot.turn.conversation_id,
        "turnId": snapshot.turn.id,
        "responseVariantId": selected.variant.id,
        "userMessageId": snapshot.user_message.id,
        "assistantMessageId": selected.assistant_message.id,
        "eventsUrl": f"/turns/{snapshot.turn.id}/events",
    }


def artifact_json(record) -> dict[str, object]:
    return {
        "id": record.id,
        "title": record.title,
        "kind": record.kind.value,
        "status": record.status.value,
        "currentVersionOrdinal": record.current_version_ordinal,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "deletedAt": record.deleted_at,
    }


def source_reference_json(reference) -> dict[str, object]:
    payload: dict[str, object] = {
        "label": reference.label,
        "type": reference.type,
        "resolved": reference.resolved,
    }
    if reference.file_id is not None:
        payload["fileId"] = reference.file_id
    if reference.file_name is not None:
        payload["fileName"] = reference.file_name
    if reference.line_range is not None:
        payload["lineRange"] = [reference.line_range[0], reference.line_range[1]]
    if reference.memory_id is not None:
        payload["memoryId"] = reference.memory_id
    if reference.memory_snippet is not None:
        payload["memorySnippet"] = reference.memory_snippet
    return payload


def artifact_version_json(
    version, *, source_references=None
) -> dict[str, object]:
    payload = {
        "id": version.id,
        "artifactId": version.artifact_id,
        "ordinal": version.ordinal,
        "content": version.content,
        "operation": version.operation.value,
        "sourceConversationId": version.source_conversation_id,
        "sourceTurnId": version.source_turn_id,
        "sourceLabels": list(version.source_labels),
        "note": version.note,
        "createdAt": version.created_at,
    }
    if source_references is not None:
        payload["sourceReferences"] = [
            source_reference_json(item) for item in source_references
        ]
    return payload


def artifact_proposal_json(proposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "conversationId": proposal.conversation_id,
        "turnId": proposal.turn_id,
        "title": proposal.title,
        "kind": proposal.kind.value,
        "content": proposal.content,
        "reason": proposal.reason,
        "status": proposal.status.value,
        "sourceLabels": list(proposal.source_labels),
        "targetArtifactId": proposal.target_artifact_id,
        "baseVersionOrdinal": proposal.base_version_ordinal,
        "createdAt": proposal.created_at,
        "updatedAt": proposal.updated_at,
        "resolvedArtifactId": proposal.resolved_artifact_id,
        "resolvedAt": proposal.resolved_at,
    }


def memory_proposal_json(proposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "conversationId": proposal.conversation_id,
        "turnId": proposal.turn_id,
        "kind": proposal.kind.value,
        "content": proposal.content,
        "reason": proposal.reason,
        "status": proposal.status.value,
        "createdAt": proposal.created_at,
        "updatedAt": proposal.updated_at,
        "resolvedMemoryId": proposal.resolved_memory_id,
        "resolvedAt": proposal.resolved_at,
    }


def memory_record_json(record) -> dict[str, object]:
    return {
        "id": record.id,
        "kind": record.kind.value,
        "content": record.content,
        "status": record.status.value,
        "sourceConversationId": record.source_conversation_id,
        "sourceTurnId": record.source_turn_id,
        "writeOrigin": record.write_origin,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "sourceProposalId": record.source_proposal_id,
        "expiredReason": record.expired_reason,
        "supersededBy": record.superseded_by,
    }
