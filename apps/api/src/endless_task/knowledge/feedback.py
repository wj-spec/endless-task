"""R5.10 引用反馈：角标点击/忽略聚合为排序权重因子。

曝光来自注入事件的 citations 明细，点击来自 citation_click 事件；
factor = 1 + click_bonus×点击数 − ignore_penalty×(曝光−点击)，
并裁剪到 [min_factor, max_factor]。只作用于 source 作用域，且按源粒度
聚合（分块的点击/曝光经 sourceId 归并到源）。聚合结果带 TTL 缓存，
检索时惰性刷新；不做复杂学习，只做简单权重调节。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional

from endless_task.domain.models import RetrievalEventKind
from endless_task.storage.sqlite_retrieval_event_repository import (
    SqliteRetrievalEventRepository,
)

logger = logging.getLogger(__name__)

#: list_recent 的仓储上限；近期事件已足够反映反馈信号。
EVENT_SCAN_LIMIT = 500


class CitationFeedbackProvider:
    """按源粒度提供排序权重因子；无信号时恒为 1.0。"""

    def __init__(
        self,
        event_repository: SqliteRetrievalEventRepository,
        *,
        source_resolver: Optional[Callable[[str], object]] = None,
        click_bonus: float = 0.05,
        ignore_penalty: float = 0.02,
        min_factor: float = 0.85,
        max_factor: float = 1.2,
        cache_ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if click_bonus < 0 or ignore_penalty < 0:
            raise ValueError("Feedback adjustments cannot be negative")
        if not 0 < min_factor <= 1 <= max_factor:
            raise ValueError("Feedback factor bounds must bracket 1.0")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self._event_repository = event_repository
        self._source_resolver = source_resolver
        self._click_bonus = click_bonus
        self._ignore_penalty = ignore_penalty
        self._min_factor = min_factor
        self._max_factor = max_factor
        self._cache_ttl = cache_ttl_seconds
        self._clock = clock
        self._factors: Dict[str, float] = {}
        self._cached_at: Optional[float] = None

    def factor(self, ref_id: str, source_id: Optional[str] = None) -> float:
        self._refresh_if_stale()
        key = self._resolve_key(ref_id, source_id)
        if key is None:
            return 1.0
        return self._factors.get(key, 1.0)

    def refresh(self) -> None:
        try:
            injections = self._event_repository.list_recent(
                limit=EVENT_SCAN_LIMIT, kind=RetrievalEventKind.INJECTION
            )
            clicks = self._event_repository.list_recent(
                limit=EVENT_SCAN_LIMIT, kind=RetrievalEventKind.CITATION_CLICK
            )
        except Exception:  # noqa: BLE001 反馈只是加分项，失败退回无信号
            logger.debug("Feedback aggregation failed", exc_info=True)
            self._factors = {}
            self._cached_at = self._clock()
            return
        impressions: Dict[str, int] = {}
        for event in injections:
            citations = (event.detail or {}).get("citations") or []
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                if citation.get("scope") != "source":
                    continue
                key = self._resolve_key(
                    str(citation.get("refId") or ""),
                    citation.get("sourceId"),
                )
                if key is not None:
                    impressions[key] = impressions.get(key, 0) + 1
        click_counts: Dict[str, int] = {}
        for event in clicks:
            detail = event.detail or {}
            if detail.get("scope") != "source":
                continue
            key = self._resolve_key(
                str(detail.get("refId") or ""), detail.get("sourceId")
            )
            if key is not None:
                click_counts[key] = click_counts.get(key, 0) + 1
        factors: Dict[str, float] = {}
        for key, impression_count in impressions.items():
            click_count = click_counts.get(key, 0)
            ignores = max(0, impression_count - click_count)
            raw = (
                1.0
                + self._click_bonus * click_count
                - self._ignore_penalty * ignores
            )
            factors[key] = max(self._min_factor, min(self._max_factor, raw))
        for key, click_count in click_counts.items():
            if key in factors:
                continue
            raw = 1.0 + self._click_bonus * click_count
            factors[key] = min(self._max_factor, raw)
        self._factors = factors
        self._cached_at = self._clock()

    def _refresh_if_stale(self) -> None:
        now = self._clock()
        if self._cached_at is None or now - self._cached_at >= self._cache_ttl:
            self.refresh()

    def _resolve_key(
        self, ref_id: str, source_id: Optional[object] = None
    ) -> Optional[str]:
        """分块引用归并到所属源；note 源引用即源自身。"""
        if source_id:
            return str(source_id)
        normalized = (ref_id or "").strip()
        if not normalized:
            return None
        if self._source_resolver is not None:
            try:
                row = self._source_resolver(normalized)
            except Exception:  # noqa: BLE001 解析失败按原 id 计
                row = None
            if row is not None:
                parent = row["source_id"] if "source_id" in row.keys() else None
                if parent:
                    return str(parent)
        return normalized
