from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, TYPE_CHECKING

from endless_task.runtime.context import (
    ApproximateTokenEstimator,
    ExtractiveConversationSummarizer,
)
from endless_task.runtime.provider import ProviderMessage

from .domain import (
    Actor,
    TranscriptEntryRecord,
    TranscriptEntryStatus,
    TranscriptEntryType,
)

if TYPE_CHECKING:
    from endless_task.runtime_v2.domain import RunRecord
    from endless_task.runtime_v2.execution import ContextCompactionResult
    from endless_task.runtime_v2.metrics import RuntimeV2MetricsCollector
    from endless_task.storage.sqlite_runtime_v2_repository import (
        SqliteRuntimeV2Repository,
    )


logger = logging.getLogger(__name__)


class TokenEstimator(Protocol):
    def estimate_text(self, content: str) -> int:
        ...

    def estimate_messages(self, messages: Sequence[ProviderMessage]) -> int:
        ...


@dataclass(frozen=True)
class _HistoryTurn:
    """一个完整轮次:user_message + 其后的 final assistant_message。"""

    entries: tuple[TranscriptEntryRecord, ...]

    @property
    def user_content(self) -> str:
        return self._content_of(TranscriptEntryType.USER_MESSAGE)

    @property
    def assistant_content(self) -> str:
        return self._content_of(TranscriptEntryType.ASSISTANT_MESSAGE)

    def _content_of(self, entry_type: TranscriptEntryType) -> str:
        for entry in self.entries:
            if entry.type is entry_type:
                content = entry.payload.get("content")
                return content if isinstance(content, str) else ""
        return ""


class RuntimeV2ContextCompactionService:
    """Entry-level context compaction for the v2 runtime.

    当模型上下文估算超过窗口阈值时,把 lane 内最早的完整轮次固化为
    ``context_summary`` entry(挂到 lane leaf 之后),记录到
    ``v2_context_compactions``,并返回 ``ContextCompactionResult`` 供执行器
    从 lane leaf 重建投影。后续投影遇到 summary entry 时跳过 covered entries。

    - 一期摘要使用本地提取式 ``ExtractiveConversationSummarizer``(零模型调用)。
    - 幂等:已记录的 covered entry 不再重复覆盖。
    - 压缩不删除原文(append-only),只通过投影层让摘要替代被覆盖前缀。
    """

    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        max_context_tokens: int,
        trigger_ratio: float = 0.8,
        target_ratio: float = 0.7,
        max_summary_tokens: int = 1024,
        estimator: Optional[TokenEstimator] = None,
        summarizer: Optional[ExtractiveConversationSummarizer] = None,
        metrics: Optional["RuntimeV2MetricsCollector"] = None,
    ) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if not 0 < trigger_ratio < 1 or not 0 < target_ratio < 1:
            raise ValueError("Compaction ratios must be within (0, 1)")
        if target_ratio >= trigger_ratio:
            raise ValueError("target_ratio must be lower than trigger_ratio")
        self._repository = repository
        self._max_context_tokens = max_context_tokens
        self._trigger_ratio = trigger_ratio
        self._target_ratio = target_ratio
        self._max_summary_tokens = max_summary_tokens
        self._estimator = estimator or ApproximateTokenEstimator()
        self._summarizer = summarizer or ExtractiveConversationSummarizer()
        self._metrics = metrics

    async def compact(
        self,
        run: "RunRecord",
        messages: Sequence[ProviderMessage],
    ) -> Optional["ContextCompactionResult"]:
        estimate = self._estimator.estimate_messages(messages)
        if estimate < self._max_context_tokens * self._trigger_ratio:
            return None

        entries = self._repository.list_lane_context_entries(run.lane_id)
        covered_ids = self._already_covered_ids(run.lane_id)
        turns = self._history_turns(entries, run_id=run.id, skip_ids=covered_ids)
        if not turns:
            return None

        # 需要释放的 token 包含摘要自身的占用:压缩后的消息 = 未覆盖历史
        # + 摘要 + 当前轮,因此原文释放量必须覆盖(估算-目标)与摘要预算。
        release_needed = (
            estimate
            - self._max_context_tokens * self._target_ratio
            + self._max_summary_tokens
        )
        selected, released = self._select_turns(turns, release_needed)
        if not selected or released <= 0:
            return None

        summary = self._summarizer.summarize(
            self._source_turns(selected),
            max_tokens=self._max_summary_tokens,
            estimator=self._estimator,
        )
        if not summary:
            return None

        covered_entry_ids = tuple(
            entry.id for turn in selected for entry in turn.entries
        )
        tokens_after = max(0, estimate - released)
        summary_entry = self._repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=run.lane_id,
            type=TranscriptEntryType.CONTEXT_SUMMARY,
            actor=Actor.RUNTIME,
            payload={
                "content": summary,
                "coveredEntryIds": list(covered_entry_ids),
                "tokensBefore": estimate,
                "tokensAfter": tokens_after,
            },
            context_policy={"include_in_llm": True, "transform": "full"},
            display={"source": "context_compaction"},
            source_run_id=run.id,
        )
        self._repository.record_context_compaction(
            conversation_id=run.conversation_id,
            lane_id=run.lane_id,
            base_entry_id=selected[0].entries[0].id,
            summary_entry_id=summary_entry.id,
            covered_entry_ids=covered_entry_ids,
            tokens_before=estimate,
            tokens_after=tokens_after,
        )
        if self._metrics is not None:
            self._metrics.record_compaction(released_tokens=released)
        logger.info(
            "Compacted %d entries for run %s (before=%d, after=%d)",
            len(covered_entry_ids),
            run.id,
            estimate,
            tokens_after,
        )
        from .execution import ContextCompactionResult

        return ContextCompactionResult(
            messages=(),
            summary_entry_id=summary_entry.id,
            covered_entry_ids=covered_entry_ids,
        )

    def _already_covered_ids(self, lane_id: str) -> set[str]:
        covered: set[str] = set()
        for record in self._repository.list_context_compactions(lane_id):
            covered.update(record.covered_entry_ids)
        return covered

    def _history_turns(
        self,
        entries: Sequence[TranscriptEntryRecord],
        *,
        run_id: str,
        skip_ids: set[str],
    ) -> list[_HistoryTurn]:
        """把共享历史按完整轮次分组(根在前)。遇到当前 run 的 entry 停止。"""
        turns: list[_HistoryTurn] = []
        current: list[TranscriptEntryRecord] = []
        for entry in entries:
            if entry.id in skip_ids:
                current = []
                continue
            if entry.source_run_id == run_id:
                break
            if entry.type is TranscriptEntryType.USER_MESSAGE:
                current = [entry]
                continue
            if (
                current
                and entry.type is TranscriptEntryType.ASSISTANT_MESSAGE
                and entry.status is TranscriptEntryStatus.FINAL
            ):
                current.append(entry)
                turns.append(_HistoryTurn(entries=tuple(current)))
                current = []
        return turns

    def _select_turns(
        self,
        turns: Sequence[_HistoryTurn],
        release_needed: int,
    ) -> tuple[list[_HistoryTurn], int]:
        """从最早的完整轮次开始覆盖,直到释放量达标。"""
        selected: list[_HistoryTurn] = []
        released = 0
        for turn in turns:
            cost = (
                self._estimator.estimate_text(turn.user_content)
                + self._estimator.estimate_text(turn.assistant_content)
                + 8
            )
            selected.append(turn)
            released += cost
            if released >= release_needed:
                break
        return selected, released

    @staticmethod
    def _source_turns(
        turns: Sequence[_HistoryTurn],
    ):
        from endless_task.runtime.context import ContextSourceTurn

        return [
            ContextSourceTurn(
                ordinal=index + 1,
                response_variant_id="",
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
            )
            for index, turn in enumerate(turns)
        ]
