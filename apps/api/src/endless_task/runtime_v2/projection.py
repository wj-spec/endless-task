"""v2 的上下文投影：把 transcript 条目按策略投影成模型消息。

**命名说明（2026-09 改名）**：这个文件原叫 `context.py`，与 `endless_task/runtime/context.py`
（v1 的 `P0ContextBuilder`，做上下文拼装）同名不同义，读代码时很难判断某个 `context`
指哪一层——`runtime_v2/compaction.py` 甚至同时用到两者。改名后语义一目了然：
**投影**（本模块）vs **拼装**（`runtime/context.py`）。

对外入口不变：`endless_task.runtime_v2` 仍从这里 re-export
`ContextPolicy` / `ContextProjection` / `ContextProjectionResult`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from endless_task.runtime.provider import (
    ProviderMessage,
    ProviderToolCall,
)

from .domain import (
    Actor,
    TranscriptEntryRecord,
    TranscriptEntryType,
)


TRUSTED = "trusted"
UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class ContextPolicy:
    include_in_llm: bool
    transform: str
    trust_level: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        entry_type: TranscriptEntryType,
    ) -> "ContextPolicy":
        default = cls.default_for(entry_type)
        include = value.get("include_in_llm", default.include_in_llm)
        transform = value.get("transform", default.transform)
        trust_level = value.get("trust_level", default.trust_level)
        return cls(
            include_in_llm=isinstance(include, bool) and include,
            transform=transform if isinstance(transform, str) else default.transform,
            trust_level=(
                trust_level if isinstance(trust_level, str) else default.trust_level
            ),
        )

    @staticmethod
    def default_for(entry_type: TranscriptEntryType) -> "ContextPolicy":
        if entry_type is TranscriptEntryType.TOOL_RESULT:
            return ContextPolicy(True, "tool_result", UNTRUSTED)
        if entry_type in (
            TranscriptEntryType.USER_MESSAGE,
            TranscriptEntryType.ASSISTANT_MESSAGE,
            TranscriptEntryType.CONTEXT_SUMMARY,
        ):
            return ContextPolicy(True, "full", TRUSTED)
        if entry_type is TranscriptEntryType.SYSTEM_NOTICE:
            return ContextPolicy(True, "full", TRUSTED)
        if entry_type is TranscriptEntryType.PLAN:
            # 执行计划由模型自己书写并固化,进入模型上下文供后续执行对照
            # (trust_level=model_output;不属于用户输入/工具输出)。
            return ContextPolicy(True, "full", TRUSTED)
        if entry_type is TranscriptEntryType.TOOL_CALL:
            return ContextPolicy(False, "none", UNTRUSTED)
        return ContextPolicy(False, "none", UNTRUSTED)


@dataclass(frozen=True)
class ContextProjectionResult:
    messages: tuple[ProviderMessage, ...]
    included_entry_ids: tuple[str, ...]
    skipped_entry_ids: tuple[str, ...]


class ContextProjection:
    """Projects stable transcript entries into provider-visible messages."""

    def project(
        self,
        entries: Sequence[TranscriptEntryRecord] | Iterable[TranscriptEntryRecord],
        *,
        prefix_messages: Sequence[ProviderMessage] = (),
    ) -> ContextProjectionResult:
        ordered = list(entries)
        # 上下文压缩:context_summary entry 携带 coveredEntryIds,被覆盖的
        # 原始 entry 不再进入模型上下文(原文保留,仅投影层跳过)。
        skip_ids: set[str] = set()
        for entry in ordered:
            if entry.type is TranscriptEntryType.CONTEXT_SUMMARY:
                covered = entry.payload.get("coveredEntryIds")
                if isinstance(covered, (list, tuple)):
                    skip_ids.update(str(item) for item in covered)

        # tool_call 条目默认不进上下文（省 token），但 tool 消息在 OpenAI 兼容协议里
        # **必须**跟在一个带 tool_calls 的 assistant 消息后面；否则请求非法（400）。
        # 因此按 callId 建索引，投影 tool_result 时按需补一条合成的 assistant 消息。
        tool_call_index: dict[str, TranscriptEntryRecord] = {}
        for entry in ordered:
            if entry.type is TranscriptEntryType.TOOL_CALL:
                call_id = _optional_string(entry.payload, "callId")
                if call_id is not None:
                    tool_call_index.setdefault(call_id, entry)

        messages: list[ProviderMessage] = list(prefix_messages)
        included: list[str] = []
        skipped: list[str] = []
        emitted_call_ids: set[str] = set()

        for entry in ordered:
            if entry.id in skip_ids:
                skipped.append(entry.id)
                continue
            policy = ContextPolicy.from_mapping(
                entry.context_policy,
                entry_type=entry.type,
            )
            if not self._is_allowed(entry, policy):
                skipped.append(entry.id)
                continue
            if entry.type is TranscriptEntryType.TOOL_RESULT:
                call_id = _optional_string(entry.payload, "callId")
                message = self._convert(entry)
                if call_id is None or message is None:
                    skipped.append(entry.id)
                    continue
                if call_id not in emitted_call_ids:
                    source = tool_call_index.get(call_id)
                    synthetic = self._convert(source) if source is not None else None
                    if synthetic is None or not synthetic.tool_calls:
                        # 连配对来源都没有：保留会破坏请求合法性，只能丢弃。
                        skipped.append(entry.id)
                        continue
                    messages.append(synthetic)
                    included.append(source.id)
                    # 该条目先被策略跳过、又因配对需要补进上下文：两者不能同时出现。
                    if source.id in skipped:
                        skipped.remove(source.id)
                    emitted_call_ids.add(call_id)
                messages.append(message)
                included.append(entry.id)
                continue
            message = self._convert(entry)
            if message is None:
                skipped.append(entry.id)
                continue
            if entry.type is TranscriptEntryType.TOOL_CALL and message.tool_calls:
                emitted_call_ids.update(call.id for call in message.tool_calls)
            messages.append(message)
            included.append(entry.id)

        return ContextProjectionResult(
            messages=tuple(messages),
            included_entry_ids=tuple(included),
            skipped_entry_ids=tuple(skipped),
        )

    def _is_allowed(
        self,
        entry: TranscriptEntryRecord,
        policy: ContextPolicy,
    ) -> bool:
        if not policy.include_in_llm or policy.transform == "none":
            return False
        if entry.actor is Actor.TOOL:
            return policy.trust_level == UNTRUSTED
        if entry.type is TranscriptEntryType.SYSTEM_NOTICE:
            return policy.trust_level == TRUSTED
        return True

    def _convert(self, entry: TranscriptEntryRecord) -> Optional[ProviderMessage]:
        if entry.type is TranscriptEntryType.USER_MESSAGE:
            return ProviderMessage(role="user", content=self._content(entry))
        if entry.type is TranscriptEntryType.ASSISTANT_MESSAGE:
            return ProviderMessage(role="assistant", content=self._content(entry))
        if entry.type is TranscriptEntryType.CONTEXT_SUMMARY:
            return ProviderMessage(role="system", content=self._content(entry))
        if entry.type is TranscriptEntryType.PLAN:
            from .plan_tool import render_plan

            return ProviderMessage(role="system", content=render_plan(entry.payload))
        if entry.type is TranscriptEntryType.SYSTEM_NOTICE:
            return ProviderMessage(role="system", content=self._content(entry))
        if entry.type is TranscriptEntryType.TOOL_RESULT:
            tool_call_id = _optional_string(entry.payload, "callId")
            tool_name = _optional_string(entry.payload, "toolName")
            if tool_call_id is None:
                return None
            return ProviderMessage(
                role="tool",
                content=self._content(entry),
                tool_call_id=tool_call_id,
                name=tool_name,
            )
        if entry.type is TranscriptEntryType.TOOL_CALL:
            call_id = _optional_string(entry.payload, "callId")
            tool_name = _optional_string(entry.payload, "toolName")
            arguments = entry.payload.get("arguments")
            if (
                call_id is None
                or tool_name is None
                or not isinstance(arguments, Mapping)
            ):
                return None
            return ProviderMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ProviderToolCall(
                        id=call_id,
                        name=tool_name,
                        arguments=dict(arguments),
                    ),
                ),
            )
        return None

    @staticmethod
    def _content(entry: TranscriptEntryRecord) -> str:
        content = entry.payload.get("content")
        return content if isinstance(content, str) else ""


def _optional_string(payload: Mapping[str, object], key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) else None
