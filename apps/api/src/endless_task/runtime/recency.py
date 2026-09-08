"""B1 记忆时间衰减 / 近期性偏置（纯计算）。

《Agentic AI Guide》第 27 章「智能体记忆系统 — 读取/检索」给出带时间项的检索
评分：

    score(d, q, t) = λ·sim(q, v_d) + (1 - λ)·exp(-(t - t_d) / τ)

其中 ``t_d`` 是记忆的更新时间，``τ`` 控制衰减速度（τ 越大衰减越慢）。

本仓库把它实现成两个正交的旋钮，便于单独配置与回退：

* ``recency_weight`` = 上式里的 ``(1 - λ)``，即**近期性占比**：0 = 纯相关性
  （默认，行为与过去一致），1 = 纯近期性；
* ``tau_days`` = 上式里的 ``τ``，单位天。

本模块只做数学与解析，不做 I/O、不看系统时钟（``now`` 由调用方传入）。
时间戳缺失/无法解析时返回 ``None``：调用方应当**退回纯相关性**，而不是把
"不知道时间的记忆"当成最新或最旧。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from endless_task.agent_platform import AgentPlatformError

DEFAULT_DECAY_TAU_DAYS = 30.0
DEFAULT_RECENCY_WEIGHT = 0.0


def parse_timestamp(value: object) -> Optional[datetime]:
    """解析 ISO 时间戳（支持末尾 ``Z``）；无法解析返回 None。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def decay_score(
    *,
    now: object,
    timestamp: object,
    tau_days: float = DEFAULT_DECAY_TAU_DAYS,
) -> Optional[float]:
    """近期性分：``exp(-age_days / tau_days)``，落在 (0, 1]。

    现在时刻得 1，一个 τ 之后约 0.37，两个 τ 之后约 0.14。时间戳缺失返回
    ``None``（调用方退回纯相关性）；未来时间戳按 1 处理。
    """
    if not isinstance(tau_days, (int, float)) or isinstance(tau_days, bool):
        raise AgentPlatformError("invalid_decay_tau", "tau_days must be a number")
    if tau_days <= 0:
        raise AgentPlatformError("invalid_decay_tau", "tau_days must be positive")
    now_dt = parse_timestamp(now)
    then_dt = parse_timestamp(timestamp)
    if now_dt is None or then_dt is None:
        return None
    age_days = (now_dt - then_dt).total_seconds() / 86_400
    if age_days <= 0:
        return 1.0
    return math.exp(-age_days / float(tau_days))


def blend_score(
    *,
    relevance: float,
    recency: Optional[float],
    recency_weight: float = DEFAULT_RECENCY_WEIGHT,
) -> float:
    """按近期性占比混合相关性分；无时间信息或未启用时等于纯相关性。"""
    if not isinstance(recency_weight, (int, float)) or isinstance(
        recency_weight, bool
    ):
        raise AgentPlatformError(
            "invalid_recency_weight", "recency_weight must be a number"
        )
    if not 0 <= float(recency_weight) <= 1:
        raise AgentPlatformError(
            "invalid_recency_weight", "recency_weight must be within [0, 1]"
        )
    if recency is None or recency_weight <= 0:
        return float(relevance)
    weight = float(recency_weight)
    return (1.0 - weight) * float(relevance) + weight * float(recency)


__all__ = [
    "DEFAULT_DECAY_TAU_DAYS",
    "DEFAULT_RECENCY_WEIGHT",
    "blend_score",
    "decay_score",
    "parse_timestamp",
]
