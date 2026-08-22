from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from typing import Any, Mapping

from endless_task.domain.models import (
    Conversation,
    ConversationSnapshot,
    Message,
    ResponseVariant,
    ResponseVariantSnapshot,
    Turn,
    TurnSnapshot,
)
from endless_task.runtime.events import RuntimeEvent


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


def conversation_snapshot_json(snapshot: ConversationSnapshot) -> dict[str, Any]:
    return {
        "conversation": conversation_json(snapshot.conversation),
        "turns": [full_turn_snapshot_json(item) for item in snapshot.turns],
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
    }
    if failure is not None and failure.response_variant_id == selected.variant.id:
        error = failure.data.get("error")
        if isinstance(error, Mapping):
            result["error"] = dict(error)
    return result


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
