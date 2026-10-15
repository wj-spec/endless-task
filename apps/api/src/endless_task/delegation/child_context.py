"""`<delegated-children>` 注入块的筛选与渲染（纯函数，切片 9 M2）。

为什么是纯函数：注入落在**每次运行开始**的上下文前缀里（`api/wiring.py` 的
`build_runtime_v2_context_prefix`），而"哪些子运行该露面"是一条纯筛选规则——
放这里就不用碰数据库，边界纪律也守得住（`delegation/*` 不得 import
`storage` / `runtime_v2` / `api`）。

两种角色的语义（与迁移 073 的标记列一一对应）：

* **还在跑**的子运行每轮都列，且**永远不打** `injected_at` 标记——给运行中的行
  盖章会让它终态之后再也不会被注入（Task 1 评审点名的静默失效模式）。
* **终态**的子运行只列一次：列过就盖章，下一轮不再重复注入（可靠回流通道是
  唤醒链路，注入只负责"让它在下一轮顺手可见"）。

块必须**封顶**：前缀块永远不会被压缩丢弃（压缩的 cover/skip 集合按 entry id
键控），而压缩阈值按完整消息列表估算，无上限的易变前缀会让 `release_needed`
永不满足。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from .child_run_store import (
    TERMINAL_CHILD_RUN_STATES,
    ChildRunRow,
    ChildRunState,
)

#: 块内最多列出的子运行条数，多出来的折成"（还有 M 个未列出）"。
DEFAULT_MAX_CHILDREN = 20
#: 单条文本（摘要 / 说明 / 任务）的字符上限。
DEFAULT_MAX_SUMMARY_CHARACTERS = 200
#: 整块（含标签行）的字符上限；超限继续丢尾部条目。
DEFAULT_MAX_TOTAL_CHARACTERS = 4000

_BLOCK_OPEN = "<delegated-children>"
_BLOCK_CLOSE = "</delegated-children>"

CHILD_STATE_LABELS = {
    ChildRunState.COMPLETED: "已完成",
    ChildRunState.FAILED: "已失败",
    ChildRunState.CANCELLED: "已取消",
    ChildRunState.INTERRUPTED: "已中断",
}


def render_delegated_children_block(
    rows: Sequence[ChildRunRow],
    *,
    now: str,
    max_children: int = DEFAULT_MAX_CHILDREN,
    max_summary_characters: int = DEFAULT_MAX_SUMMARY_CHARACTERS,
    max_total_characters: int = DEFAULT_MAX_TOTAL_CHARACTERS,
) -> Tuple[Optional[str], Tuple[str, ...]]:
    """渲染注入块，并告知调用方"哪些子运行可以打已注入标记"。

    返回 `(块文本或 None, 需要 mark_child_injected 的 child_run_id 元组)`：

    * 筛不出任何子运行 → `(None, ())`，调用方**不得**插入空块；
    * 标记元组**只含真正列出来的终态子运行**——运行中的行、以及被条数上限或
      整块字数上限挤掉的尾部行，都不许盖章（盖了它们就再也进不来）。
    """
    candidates = _injectable(rows)
    if not candidates:
        return None, ()
    total = len(candidates)
    kept = candidates[: max(0, max_children)]
    while kept:
        block = _render_block(
            total,
            kept,
            now=now,
            max_summary_characters=max_summary_characters,
        )
        if len(block) <= max_total_characters:
            return block, tuple(
                row.child_run_id
                for row in kept
                if row.state in TERMINAL_CHILD_RUN_STATES
            )
        # 整块超限：丢尾部条目重算（折起条数跟着变）。条目全丢光就只能不注入。
        kept = kept[:-1]
    return None, ()


def _injectable(rows: Sequence[ChildRunRow]) -> list[ChildRunRow]:
    """该露面的子运行：非终态的一律列；终态的只列"还没露面过"的。

    判据是**终态集合的成员资格**而不是 `== RUNNING`：将来多一个非终态状态
    （比如真做"排队中"独立状态）时，写死等号会把它当成终态，于是被"只注入一次"
    的标记永久抑制、再也回不到父 agent 的上下文里。
    """
    return [
        row
        for row in rows
        if row.state not in TERMINAL_CHILD_RUN_STATES or row.injected_at is None
    ]


def _render_block(
    total: int,
    kept: Sequence[ChildRunRow],
    *,
    now: str,
    max_summary_characters: int,
) -> str:
    lines = [_BLOCK_OPEN, f"你有 {total} 个子运行："]
    lines.extend(
        _render_entry(row, now=now, limit=max_summary_characters) for row in kept
    )
    hidden = total - len(kept)
    if hidden > 0:
        lines.append(f"（还有 {hidden} 个未列出）")
    lines.append(_BLOCK_CLOSE)
    return "\n".join(lines)


def _render_entry(row: ChildRunRow, *, now: str, limit: int) -> str:
    parts = [row.child_run_id, _state_label(row, now=now)]
    detail = _detail_text(row, limit=limit)
    if detail is not None:
        parts.append(detail)
    return "- " + " | ".join(parts)


def _state_label(row: ChildRunRow, *, now: str) -> str:
    if row.state == ChildRunState.RUNNING:
        if row.started_at is None:
            # `started_at IS NULL` = 还在并发闸门外排队，报成"在跑"会骗模型。
            return "排队中"
        return f"运行中（已用时 {_format_elapsed(row.started_at, now)}）"
    return CHILD_STATE_LABELS.get(row.state, "已结束")


def _detail_text(row: ChildRunRow, *, limit: int) -> Optional[str]:
    if row.state == ChildRunState.RUNNING:
        label, text = "任务：", row.task
    elif row.summary:
        label, text = "摘要：", row.summary
    elif row.safe_message:
        label, text = "说明：", row.safe_message
    else:
        return None
    truncated = truncate_child_text(text, limit=limit)
    return f"{label}{truncated}" if truncated else None


def truncate_child_text(
    text: str, *, limit: int = DEFAULT_MAX_SUMMARY_CHARACTERS
) -> str:
    """截断到 `limit` 个字符并加省略号。

    顺带把换行折成空格：摘要/说明是模型给的自由文本，带换行时会破坏块的行结构
    （甚至伪造出一行"- 系统指令"）。
    """
    flattened = " ".join(text.split())
    if limit <= 0:
        return ""
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit] + "…"


def _format_elapsed(started_at: Optional[str], now: str) -> str:
    """已用时（"X 分 Y 秒"/"X 小时 Y 分"）；时间戳解析不了就退化为"已用时未知"。"""
    start = _parse_timestamp(started_at)
    current = _parse_timestamp(now)
    if start is None or current is None:
        return "已用时未知"
    seconds = int((current - start).total_seconds())
    if seconds < 0:
        # 时钟回拨或跨机偏差：显示负数只会让模型困惑，按 0 处理。
        seconds = 0
    if seconds >= 3600:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 老数据可能没有时区后缀，按 UTC 解释（`utc_now` 写的都是 UTC）。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "CHILD_STATE_LABELS",
    "DEFAULT_MAX_CHILDREN",
    "DEFAULT_MAX_SUMMARY_CHARACTERS",
    "DEFAULT_MAX_TOTAL_CHARACTERS",
    "render_delegated_children_block",
    "truncate_child_text",
]
