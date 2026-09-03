"""Structured context checkpoints (M2 AP-204).

Model-generated compaction summaries are exchanged as strict JSON first and
typed values second (doc 04 §4.4):

1. the model emits a JSON object conforming to ``CHECKPOINT_JSON_SCHEMA``
   (Draft 2020-12, validated with ``jsonschema``),
2. ``parse_checkpoint_json`` validates the raw text (JSON + schema) and only
   then builds the typed :class:`StructuredContextCheckpoint`,
3. a parse/schema failure raises a structured error, so the caller can keep
   the previous checkpoint untouched (validation failures never overwrite
   good state),
4. ``render_checkpoint_message`` turns the typed value back into a
   deterministic system message for the model,
5. ``checkpoint_entry_conflicts`` detects covered entries that would
   otherwise re-enter the model together with the checkpoint (hard rule 4).

No runtime store or provider is touched here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)
from endless_task.runtime.provider import ProviderMessage

CHECKPOINT_SCHEMA_VERSION = 1

_MAX_SECTION_ITEMS = 64
_MAX_SECTION_TEXT_LENGTH = 1_000
_MAX_PATH_LENGTH = 1_024
_MAX_GOAL_LENGTH = 512
_SHA256_HEX = frozenset("0123456789abcdef")


def _section_schema() -> dict[str, Any]:
    item = {
        "type": "string",
        "minLength": 1,
        "maxLength": _MAX_SECTION_TEXT_LENGTH,
    }
    return {
        "type": "array",
        "items": item,
        "maxItems": _MAX_SECTION_ITEMS,
        "uniqueItems": True,
    }


def _ref_schema(extra: Mapping[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_LENGTH}},
        "required": ["path"],
        "additionalProperties": False,
    }
    schema["properties"].update(extra)
    return schema


def _ref_array_schema(extra: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _ref_schema(extra),
        "maxItems": _MAX_SECTION_ITEMS,
    }


#: JSON Schema a model-generated checkpoint must satisfy.
CHECKPOINT_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "goal": {"type": "string", "minLength": 1, "maxLength": _MAX_GOAL_LENGTH},
        "constraints": _section_schema(),
        "progress": _section_schema(),
        "decisions": _section_schema(),
        "open_questions": _section_schema(),
        "files_read": _ref_array_schema({"sha256": {"type": ["string", "null"], "maxLength": 64}}),
        "files_modified": _ref_array_schema({"sha256": {"type": ["string", "null"], "maxLength": 64}}),
        "active_effects": _ref_array_schema({}),
        "next_steps": _section_schema(),
        "critical_context": _section_schema(),
        "covered_entry_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
            "maxItems": _MAX_SECTION_ITEMS,
            "uniqueItems": True,
        },
        "source_checkpoint_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": 256,
        },
    },
    "required": [
        "goal",
        "constraints",
        "progress",
        "decisions",
        "open_questions",
        "files_read",
        "files_modified",
        "active_effects",
        "next_steps",
        "critical_context",
        "covered_entry_ids",
        "source_checkpoint_id",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CheckpointFileRef:
    path: str
    sha256: Optional[str] = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CHECKPOINT_SCHEMA_VERSION,
            protocol="checkpoint_file_ref",
        )
        object.__setattr__(
            self,
            "path",
            require_text(self.path, field_name="path", max_length=_MAX_PATH_LENGTH),
        )
        if self.sha256 is not None:
            object.__setattr__(
                self,
                "sha256",
                _validated_sha256(self.sha256),
            )


@dataclass(frozen=True)
class CheckpointEffectRef:
    path: str
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CHECKPOINT_SCHEMA_VERSION,
            protocol="checkpoint_effect_ref",
        )
        object.__setattr__(
            self,
            "path",
            require_text(self.path, field_name="path", max_length=_MAX_PATH_LENGTH),
        )


def _validated_sections(values: Collection[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must be a collection of strings",
        )
    try:
        normalized = tuple(
            require_text(
                value,
                field_name=field_name,
                max_length=_MAX_SECTION_TEXT_LENGTH,
            )
            for value in values
        )
    except TypeError as error:
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must be a collection of strings",
        ) from error
    if len(normalized) > _MAX_SECTION_ITEMS:
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} exceeds the item limit",
        )
    if len(set(normalized)) != len(normalized):
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must not contain duplicates",
        )
    return normalized


def _validated_ids(values: Collection[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must be a collection of identifiers",
        )
    try:
        normalized = tuple(
            require_identifier(value, field_name=field_name, max_length=256)
            for value in values
        )
    except TypeError as error:
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must be a collection of identifiers",
        ) from error
    if len(normalized) > _MAX_SECTION_ITEMS:
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} exceeds the item limit",
        )
    if len(set(normalized)) != len(normalized):
        raise AgentPlatformError(
            "invalid_checkpoint",
            f"{field_name} must not contain duplicates",
        )
    return normalized


def _validated_sha256(value: str) -> str:
    normalized = require_text(value, field_name="sha256", max_length=64)
    if len(normalized) != 64 or any(
        character not in _SHA256_HEX for character in normalized
    ):
        raise AgentPlatformError(
            "invalid_checkpoint",
            "sha256 must be a 64-character hex digest",
        )
    return normalized


@dataclass(frozen=True)
class StructuredContextCheckpoint:
    goal: str
    constraints: tuple[str, ...]
    progress: tuple[str, ...]
    decisions: tuple[str, ...]
    open_questions: tuple[str, ...]
    files_read: tuple[CheckpointFileRef, ...]
    files_modified: tuple[CheckpointFileRef, ...]
    active_effects: tuple[CheckpointEffectRef, ...]
    next_steps: tuple[str, ...]
    critical_context: tuple[str, ...]
    covered_entry_ids: tuple[str, ...]
    source_checkpoint_id: Optional[str] = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CHECKPOINT_SCHEMA_VERSION,
            protocol="structured_context_checkpoint",
        )
        object.__setattr__(
            self,
            "goal",
            require_text(self.goal, field_name="goal", max_length=_MAX_GOAL_LENGTH),
        )
        for field_name in (
            "constraints",
            "progress",
            "decisions",
            "open_questions",
            "next_steps",
            "critical_context",
        ):
            object.__setattr__(
                self,
                field_name,
                _validated_sections(getattr(self, field_name), field_name=field_name),
            )
        for field_name in ("files_read", "files_modified"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, CheckpointFileRef) for value in values
            ):
                raise AgentPlatformError(
                    "invalid_checkpoint",
                    f"{field_name} must be a tuple of CheckpointFileRef values",
                )
            if len(values) > _MAX_SECTION_ITEMS:
                raise AgentPlatformError(
                    "invalid_checkpoint",
                    f"{field_name} exceeds the item limit",
                )
        active_effects = self.active_effects
        if not isinstance(active_effects, tuple) or any(
            not isinstance(value, CheckpointEffectRef) for value in active_effects
        ):
            raise AgentPlatformError(
                "invalid_checkpoint",
                "active_effects must be a tuple of CheckpointEffectRef values",
            )
        object.__setattr__(
            self,
            "covered_entry_ids",
            _validated_ids(self.covered_entry_ids, field_name="covered_entry_id"),
        )
        if self.source_checkpoint_id is not None:
            object.__setattr__(
                self,
                "source_checkpoint_id",
                require_identifier(
                    self.source_checkpoint_id,
                    field_name="source_checkpoint_id",
                    max_length=256,
                ),
            )


def parse_checkpoint_json(text: str) -> StructuredContextCheckpoint:
    """Validate raw model JSON and build a typed checkpoint.

    Any JSON/schema/type failure raises a structured error so callers keep
    the previous checkpoint intact.
    """
    if not isinstance(text, str):
        raise AgentPlatformError(
            "invalid_checkpoint_json",
            "Checkpoint input must be text",
        )
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as error:
        raise AgentPlatformError(
            "invalid_checkpoint_json",
            "模型返回的 checkpoint 不是有效 JSON。",
            retryable=False,
            details={"error": type(error).__name__},
        ) from error
    if not isinstance(raw, Mapping):
        raise AgentPlatformError(
            "invalid_checkpoint_json",
            "模型返回的 checkpoint 必须是 JSON 对象。",
            retryable=False,
        )
    try:
        Draft202012Validator(CHECKPOINT_JSON_SCHEMA).validate(dict(raw))
    except ValidationError as error:
        raise AgentPlatformError(
            "invalid_checkpoint_schema",
            "模型返回的 checkpoint 不符合结构化协议。",
            retryable=False,
            details={
                "path": "/".join(str(part) for part in error.absolute_path),
                "message": error.message,
            },
        ) from error

    def file_refs(items: Any) -> tuple[CheckpointFileRef, ...]:
        return tuple(
            CheckpointFileRef(
                path=str(item["path"]),
                sha256=item.get("sha256"),
            )
            for item in items
        )

    effect_refs = tuple(
        CheckpointEffectRef(path=str(item["path"])) for item in raw["active_effects"]
    )
    return StructuredContextCheckpoint(
        goal=raw["goal"],
        constraints=raw["constraints"],
        progress=raw["progress"],
        decisions=raw["decisions"],
        open_questions=raw["open_questions"],
        files_read=file_refs(raw["files_read"]),
        files_modified=file_refs(raw["files_modified"]),
        active_effects=effect_refs,
        next_steps=raw["next_steps"],
        critical_context=raw["critical_context"],
        covered_entry_ids=raw["covered_entry_ids"],
        source_checkpoint_id=raw.get("source_checkpoint_id"),
    )


def render_checkpoint_message(
    checkpoint: StructuredContextCheckpoint,
) -> ProviderMessage:
    """Deterministic system-message rendering of a typed checkpoint."""
    if not isinstance(checkpoint, StructuredContextCheckpoint):
        raise AgentPlatformError(
            "invalid_checkpoint",
            "Checkpoint rendering requires a StructuredContextCheckpoint",
        )

    def section(title: str, items: Collection[str]) -> list[str]:
        if not items:
            return []
        return [f"{title}:"] + [f"- {item}" for item in items]

    lines = [f"任务目标：{checkpoint.goal}"]
    for title, items in (
        ("约束", checkpoint.constraints),
        ("进展", checkpoint.progress),
        ("已做决策", checkpoint.decisions),
        ("待确认问题", checkpoint.open_questions),
        ("关键上下文", checkpoint.critical_context),
        ("下一步", checkpoint.next_steps),
    ):
        lines.extend(section(title, items))
    if checkpoint.files_read:
        lines.append("已读文件：")
        lines.extend(f"- {ref.path}" for ref in checkpoint.files_read)
    if checkpoint.files_modified:
        lines.append("已修改文件：")
        lines.extend(f"- {ref.path}" for ref in checkpoint.files_modified)
    if checkpoint.active_effects:
        lines.append("进行中的效果：")
        lines.extend(f"- {ref.path}" for ref in checkpoint.active_effects)
    lines.append(f"覆盖条目：{len(checkpoint.covered_entry_ids)} 条；"
                 f"来源 checkpoint：{checkpoint.source_checkpoint_id or '无'}")
    return ProviderMessage(role="system", content="\n".join(lines))


def checkpoint_entry_conflicts(
    covered_entry_ids: Collection[str],
    active_entry_ids: Collection[str],
) -> tuple[str, ...]:
    """Covered entries that must not re-enter the model with the checkpoint."""
    covered = _validated_ids(covered_entry_ids, field_name="covered_entry_id")
    active = _validated_ids(active_entry_ids, field_name="active_entry_id")
    return tuple(sorted(set(covered) & set(active)))


__all__ = [
    "CHECKPOINT_JSON_SCHEMA",
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointEffectRef",
    "CheckpointFileRef",
    "StructuredContextCheckpoint",
    "checkpoint_entry_conflicts",
    "parse_checkpoint_json",
    "render_checkpoint_message",
]
