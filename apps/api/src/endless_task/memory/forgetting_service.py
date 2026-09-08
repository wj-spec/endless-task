"""B3 记忆遗忘服务：按重要性/访问/时间挑选该忘的记忆。

策略与边界（与《Agentic AI Guide》第 27 章「遗忘机制」对齐）：

* 概率来自 :mod:`endless_task.runtime.memory_forgetting`（纯计算，可解释）；
* **钉住的记忆永不自动遗忘**；
* **重要记忆（importance ≥ REVIEW_IMPORTANCE）不静默删除**，只进入"待确认"列表，
  由用户在记忆面板决定保留（钉住）还是放弃；
* 每轮有上限，避免一次性抹掉大量记忆。

服务只做编排与落库，不直接读时钟以外的外部状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from endless_task.domain.models import MemoryRecord
from endless_task.runtime.memory_forgetting import (
    DEFAULT_TAU_DAYS,
    FORGET_THRESHOLD,
    REVIEW_IMPORTANCE,
    ForgettableMemory,
    select_forgettable,
    select_needs_review,
)
from endless_task.storage import SqliteMemoryRepository

FORGOTTEN_REASON = "forgotten_low_value"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MemoryForgettingReport:
    """一次遗忘扫描的结果（``dry_run`` 时只预览不动数据）。"""

    forgotten: tuple[MemoryRecord, ...] = ()
    needs_review: tuple[ForgettableMemory, ...] = ()
    dry_run: bool = False

    @property
    def forgotten_count(self) -> int:
        return len(self.forgotten)

    def as_json(self) -> dict[str, object]:
        return {
            "dryRun": self.dry_run,
            "forgottenCount": self.forgotten_count,
            "forgotten": [
                {
                    "memoryId": record.id,
                    "content": record.content,
                    "importance": record.importance,
                    "accessCount": record.access_count,
                    "pinned": record.pinned,
                }
                for record in self.forgotten
            ],
            "needsReview": [
                {**item.as_json(), "needsReview": True}
                for item in self.needs_review
            ],
        }


class MemoryForgettingService:
    def __init__(
        self,
        *,
        memory_repository: SqliteMemoryRepository,
        clock: Callable[[], datetime] = _utc_now,
        tau_days: float = DEFAULT_TAU_DAYS,
        threshold: float = FORGET_THRESHOLD,
        max_per_run: int = 10,
    ) -> None:
        if max_per_run <= 0:
            raise ValueError("max_per_run must be positive")
        self._memory_repository = memory_repository
        self._clock = clock
        self._tau_days = tau_days
        self._threshold = threshold
        self._max_per_run = max_per_run

    def _records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._memory_repository.list_memories())

    def _forgettable(self) -> tuple[ForgettableMemory, ...]:
        """低价值候选：概率达阈值，且不属于"重要到该问一句"的记忆。"""
        review_ids = {
            item.record.id
            for item in select_needs_review(
                self._records(), now=self._clock(), tau_days=self._tau_days
            )
        }
        return tuple(
            item
            for item in select_forgettable(
                self._records(),
                now=self._clock(),
                threshold=self._threshold,
                tau_days=self._tau_days,
            )
            if item.record.id not in review_ids
            and getattr(item.record, "importance", 0.0) < REVIEW_IMPORTANCE
        )

    def _needs_review(self) -> tuple[ForgettableMemory, ...]:
        return select_needs_review(
            self._records(), now=self._clock(), tau_days=self._tau_days
        )

    def preview(self) -> MemoryForgettingReport:
        """只读预览：哪些会被忘、哪些需要人工确认。"""
        return MemoryForgettingReport(
            forgotten=tuple(
                item.record for item in self._forgettable()[: self._max_per_run]
            ),
            needs_review=self._needs_review(),
            dry_run=True,
        )

    def run(self) -> MemoryForgettingReport:
        """执行遗忘：低价值记忆软过期；重要记忆只报告、不删除。"""
        forgotten: list[MemoryRecord] = []
        for item in self._forgettable():
            if len(forgotten) >= self._max_per_run:
                break
            record = self._memory_repository.expire_memory(
                item.record.id,
                reason=FORGOTTEN_REASON,
            )
            forgotten.append(record)
        return MemoryForgettingReport(
            forgotten=tuple(forgotten),
            needs_review=self._needs_review(),
            dry_run=False,
        )


__all__ = [
    "FORGOTTEN_REASON",
    "MemoryForgettingReport",
    "MemoryForgettingService",
]
