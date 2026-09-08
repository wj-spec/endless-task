"""B3 重要性加权遗忘 / 间隔重复（纯计算）。

《Agentic AI Guide》第 27 章「智能体记忆系统 — 更新：遗忘机制」给了三条线索：

* LRU 驱逐：容量超限时先丢最近最少使用的；
* 重要性加权遗忘：``p(forget | d) ∝ exp(-importance(d))``；
* 间隔重复：反复被访问的记忆保留更久（指数遗忘曲线）。

本模块把三者合成一个可解释的概率：

    p(forget) = exp(-2·importance)
              · exp(-0.5·ln(1 + access_count))
              · (1 - exp(-age_days / τ))

* 重要（importance 高）→ 概率指数下降；
* 常用（access_count 高）→ 概率对数下降（间隔重复）；
* 越久没动（age 大）→ 概率上升，τ 控制速度；
* **钉住（pinned）→ 概率恒为 0**，任何自动遗忘都不许碰。

纯计算、无 I/O、无系统时钟（``now`` 由调用方传入）。删除动作由仓储/服务负责，
且**重要记忆在遗忘前必须走确认**（``needs_review``），不允许静默删除。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

DEFAULT_IMPORTANCE = 0.5
DEFAULT_TAU_DAYS = 30.0
DEFAULT_EXPIRY_BASE_DAYS = 30.0

#: p(forget) 达到该值即进入"可遗忘"候选。
FORGET_THRESHOLD = 0.5

#: 重要性达到该值的记忆，遗忘前必须人工确认。
REVIEW_IMPORTANCE = 0.7

# 重要性权重取 1.0：默认重要性 0.5 的老记忆最大遗忘概率 ≈ 0.61（会被忘），
# 重要性 ≥ 0.7 的记忆最大概率 ≤ 0.50（不会被自动遗忘，只进"待确认"）。
_IMPORTANCE_WEIGHT = 1.0
_ACCESS_WEIGHT = 0.5


def clamp_importance(value: object) -> float:
    """重要性归一到 [0, 1]。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AgentPlatformError(
            "invalid_memory_importance", "importance must be a number"
        )
    return min(1.0, max(0.0, float(value)))


def _require_non_negative(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", f"{field_name} must be a number"
        )
    if value < 0:
        raise AgentPlatformError(
            "invalid_memory_forgetting_input",
            f"{field_name} must be non-negative",
        )
    return float(value)


def forget_probability(
    *,
    importance: float,
    access_count: int,
    age_days: float,
    pinned: bool = False,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> float:
    """被自动遗忘的概率（0–1）；钉住恒为 0，未老化也为 0。"""
    if pinned:
        return 0.0
    importance_value = clamp_importance(importance)
    accesses = _require_non_negative(access_count, "access_count")
    age = _require_non_negative(age_days, "age_days")
    tau = _require_non_negative(tau_days, "tau_days")
    if tau <= 0:
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", "tau_days must be positive"
        )
    if age <= 0:
        return 0.0
    importance_factor = math.exp(-_IMPORTANCE_WEIGHT * importance_value)
    access_factor = math.exp(-_ACCESS_WEIGHT * math.log1p(accesses))
    age_factor = 1.0 - math.exp(-age / tau)
    return min(1.0, max(0.0, importance_factor * access_factor * age_factor))


def retention_score(
    *,
    importance: float,
    access_count: int,
    age_days: float,
    pinned: bool = False,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> float:
    """保留分 = 1 - 遗忘概率（越大越该留）。"""
    return 1.0 - forget_probability(
        importance=importance,
        access_count=access_count,
        age_days=age_days,
        pinned=pinned,
        tau_days=tau_days,
    )


def next_expiry_days(
    *,
    importance: float,
    access_count: int,
    base_days: float = DEFAULT_EXPIRY_BASE_DAYS,
) -> float:
    """间隔重复：访问越多、越重要，下次过期越晚。"""
    base = _require_non_negative(base_days, "base_days")
    if base <= 0:
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", "base_days must be positive"
        )
    accesses = _require_non_negative(access_count, "access_count")
    importance_value = clamp_importance(importance)
    return round(base * (1.0 + accesses) * (1.0 + importance_value), 4)


def needs_review(
    *,
    importance: float,
    age_days: float,
    pinned: bool = False,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> bool:
    """重要记忆"久未使用"时必须先确认，不能静默留在库里。

    重要记忆的遗忘概率天然很低（p ∝ exp(-2·importance)），所以不能用概率阈值
    来判断"该不该问一句"——判据是**重要 + 已经一个 τ 没被用到**。
    """
    if pinned:
        return False
    age = _require_non_negative(age_days, "age_days")
    tau = _require_non_negative(tau_days, "tau_days")
    if tau <= 0:
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", "tau_days must be positive"
        )
    return (
        clamp_importance(importance) >= REVIEW_IMPORTANCE
        and age >= tau
    )


@dataclass(frozen=True)
class ForgettableMemory:
    """一条"可遗忘"候选（含概率，便于解释与预览）。"""

    record: object
    probability: float

    def as_json(self) -> dict[str, object]:
        record = self.record
        return {
            "memoryId": getattr(record, "id", ""),
            "content": getattr(record, "content", ""),
            "importance": clamp_importance(getattr(record, "importance", DEFAULT_IMPORTANCE)),
            "accessCount": int(getattr(record, "access_count", 0) or 0),
            "pinned": bool(getattr(record, "pinned", False)),
            "probability": round(self.probability, 4),
        }


def _age_days(record: object, now: datetime) -> Optional[float]:
    """记忆"多久没被用到"：优先 last_accessed_at，其次 updated_at。"""
    from endless_task.runtime.recency import parse_timestamp

    stamp = parse_timestamp(getattr(record, "last_accessed_at", None)) or (
        parse_timestamp(getattr(record, "updated_at", None))
    )
    if stamp is None:
        return None
    return max(0.0, (now - stamp).total_seconds() / 86_400)


def select_needs_review(
    records: Iterable[object],
    *,
    now: datetime,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> tuple[ForgettableMemory, ...]:
    """挑出"重要但久未使用"、应当请用户确认的记忆（按概率高→低）。"""
    candidates: list[ForgettableMemory] = []
    for record in records:
        age = _age_days(record, now)
        if age is None:
            continue
        importance = getattr(record, "importance", DEFAULT_IMPORTANCE)
        if not needs_review(
            importance=importance,
            age_days=age,
            pinned=bool(getattr(record, "pinned", False)),
            tau_days=tau_days,
        ):
            continue
        candidates.append(
            ForgettableMemory(
                record=record,
                probability=forget_probability(
                    importance=importance,
                    access_count=int(getattr(record, "access_count", 0) or 0),
                    age_days=age,
                    tau_days=tau_days,
                ),
            )
        )
    candidates.sort(key=lambda item: item.probability, reverse=True)
    return tuple(candidates)


def select_forgettable(
    records: Iterable[object],
    *,
    now: datetime,
    threshold: float = FORGET_THRESHOLD,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> tuple[ForgettableMemory, ...]:
    """挑出可遗忘候选，按概率从高到低排序（时间戳不可解析的跳过）。"""
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", "threshold must be a number"
        )
    if not 0 <= float(threshold) <= 1:
        raise AgentPlatformError(
            "invalid_memory_forgetting_input", "threshold must be within [0, 1]"
        )
    candidates: list[ForgettableMemory] = []
    for record in records:
        age = _age_days(record, now)
        if age is None:
            continue
        probability = forget_probability(
            importance=getattr(record, "importance", DEFAULT_IMPORTANCE),
            access_count=int(getattr(record, "access_count", 0) or 0),
            age_days=age,
            pinned=bool(getattr(record, "pinned", False)),
            tau_days=tau_days,
        )
        if probability >= float(threshold) and probability > 0:
            candidates.append(ForgettableMemory(record=record, probability=probability))
    candidates.sort(key=lambda item: item.probability, reverse=True)
    return tuple(candidates)


__all__ = [
    "DEFAULT_EXPIRY_BASE_DAYS",
    "DEFAULT_IMPORTANCE",
    "DEFAULT_TAU_DAYS",
    "FORGET_THRESHOLD",
    "ForgettableMemory",
    "REVIEW_IMPORTANCE",
    "clamp_importance",
    "forget_probability",
    "needs_review",
    "next_expiry_days",
    "retention_score",
    "select_forgettable",
    "select_needs_review",
]
