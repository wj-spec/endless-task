"""@ 引用会话的**有界投影**（纯函数：不读库、不写库、不调模型）。

投影按以下三档限制上下文预算：

============  ==========================  =====================================
档位          单条上限                    说明
============  ==========================  =====================================
``summary``   ≤1.5K token（默认）          最近 2 轮 + 更早要点 + 产物名
``recent``    ≤4K token（``recentTurns`` 取 5 / 10）
``full``      硬天花板 16K（默认禁用）     该 lane 的**全部**轮次
============  ==========================  =====================================

三条硬约束（写死在实现里，不靠调用方自觉）：

1. **单位如实**：token 数是既有启发式 ``context_engine.planner.estimate_text_tokens``
   （``ceil(len(text)/3)``）的**估算**，不是 provider 的真实计费；本模块不新造估算器。
2. **不许静默丢**：单条超过本档 ``max_tokens`` 时，按固定阶梯削减，并**必须**把
   ``truncated`` 置真——调用方据此写入 provenance 并（前端）告警。
3. **确定性**：同一输入必然得到逐字节相同的 ``text``。

**"最近 N 轮"的轮次口径不在这里另立**：调用方用 ``runtime_v2.compaction`` 的
``history_turns``（抽取出的共用纯函数）把 entry 链折成轮次，再喂 ``TurnView``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from endless_task.context_engine.planner import estimate_text_tokens

# ---------------------------------------------------------------------------
# 档位与预算常量（02 §3.1）
# ---------------------------------------------------------------------------

MODE_SUMMARY = "summary"
MODE_RECENT = "recent"
MODE_FULL = "full"

#: 合法档位（顺序即文档顺序）。
REFERENCE_MODES: tuple[str, ...] = (MODE_SUMMARY, MODE_RECENT, MODE_FULL)

#: 被引用对象的形态，进入块的 ``kind=`` 属性。
REFERENCE_KINDS: tuple[str, ...] = ("normal", "ephemeral", "branch")

EPHEMERAL_KIND = "ephemeral"

#: ``full`` 档的**硬天花板**。文档口径是"无固定上限"，但注入必须是有界的：
#: 这里给一个显式天花板，实际生效值还要与"单轮总量预算"取小（见 ``max_tokens``）。
FULL_MODE_TOKEN_CEILING = 16_384

#: 档位 → 单条 token 上限。
MODE_TOKEN_LIMITS: dict[str, int] = {
    MODE_SUMMARY: 1_500,
    MODE_RECENT: 4_000,
    MODE_FULL: FULL_MODE_TOKEN_CEILING,
}

#: 摘要档固定的"最近 N 轮"。
SUMMARY_TURN_WINDOW = 2

#: ``recent`` 档可选（且仅可）的 N（picker 二选一）。
RECENT_MODE_TURN_CHOICES: tuple[int, ...] = (5, 10)

#: 单轮总量上限 ≈4K token（02 §3.1："约默认 131K 窗口的 3%"）。
DEFAULT_TOTAL_TOKEN_BUDGET = 4_096

#: 单轮默认引用条数；**第 3 条起只告警不拒绝**（02 §3.1："第 3 条起在 UI 上提示"）。
DEFAULT_REFERENCE_COUNT = 2

#: 单轮总量预算剩不到这个数时，**宁可整条不注入**，也不塞一条几十 token 的残块
#: （残块没有信息量，却占一行上下文并让用户误以为"引用已生效"）。命中此界即告警。
MIN_REFERENCE_TOKENS = 128

#: 单条文本的字数上限（02 §3.1 逐字口径）。
USER_TURN_CHARS = 400
ASSISTANT_TURN_CHARS = 800
EARLIER_BULLET_CHARS = 120
#: "更早的要点"最多几条。
MAX_EARLIER_BULLETS = 8
#: 削减阶梯里文本字数下限（再小没有信息量，直接走硬截断）。
MIN_TEXT_CAP = 8

EMPTY_MARKER = "（无）"
TRUNCATION_MARKER = "\n…[已按预算截断]"

_ATTRIBUTE_UNSAFE = ('"', "\n", "\r", "\t")


class ReferenceProjectionError(ValueError):
    """投影参数非法。``field`` 是**要指名给用户**的字段名（路由层据此回 4xx）。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True)
class TurnView:
    """一个**完整轮次**的只读视图（user 原文 + 最终 assistant 回答）。

    由调用方用 ``runtime_v2.compaction.history_turns`` 分组得到——轮次口径只此一处。
    """

    user_content: str
    assistant_content: str


@dataclass(frozen=True)
class ReferenceProjection:
    """一条被引用会话的有界投影。``text`` 即注入 entry 的正文。"""

    text: str
    token_estimate: int
    truncated: bool
    mode: str
    max_tokens: int
    #: 实际渲染进 ``[最近 N 轮]`` 的轮数。
    rendered_turns: int
    #: 实际渲染进 ``[更早的要点]`` 的条数。
    rendered_earlier_bullets: int
    #: 实际渲染进 ``[触及的产物/文件]`` 的条数。
    rendered_artifacts: int

    @property
    def character_count(self) -> int:
        return len(self.text)


# ---------------------------------------------------------------------------
# 参数校验（纯函数，路由层复用同一份口径）
# ---------------------------------------------------------------------------


def normalize_mode(mode: Optional[str]) -> str:
    """档位规范化：缺省 = ``summary``（02 §3.1"摘要（默认）"）。"""

    if mode is None:
        return MODE_SUMMARY
    if mode not in REFERENCE_MODES:
        raise ReferenceProjectionError(
            "mode",
            "unknown mode "
            f"{mode!r}; expected one of {', '.join(REFERENCE_MODES)}",
        )
    return mode


def resolve_recent_turns(*, mode: str, recent_turns: Optional[int]) -> Optional[int]:
    """校验并返回该档位实际生效的"最近 N 轮"窗口。

    - ``summary``：固定 2 轮；显式给 ``recentTurns`` 时只接受 ``2``。
    - ``recent``：必须落在 :data:`RECENT_MODE_TURN_CHOICES`（5 / 10），缺省 5。
    - ``full``：整段，不接受 ``recentTurns``。

    越界一律 :class:`ReferenceProjectionError`（字段名 ``recentTurns``）。
    """

    normalized = normalize_mode(mode)
    if normalized == MODE_FULL:
        if recent_turns is not None:
            raise ReferenceProjectionError(
                "recentTurns",
                "must be omitted when mode is 'full' (full projects every turn)",
            )
        return None
    if normalized == MODE_SUMMARY:
        if recent_turns is None:
            return SUMMARY_TURN_WINDOW
        if recent_turns != SUMMARY_TURN_WINDOW:
            raise ReferenceProjectionError(
                "recentTurns",
                f"must be {SUMMARY_TURN_WINDOW} for mode 'summary' "
                f"(got {recent_turns})",
            )
        return SUMMARY_TURN_WINDOW
    if recent_turns is None:
        return RECENT_MODE_TURN_CHOICES[0]
    if recent_turns not in RECENT_MODE_TURN_CHOICES:
        raise ReferenceProjectionError(
            "recentTurns",
            "must be one of "
            f"{', '.join(str(value) for value in RECENT_MODE_TURN_CHOICES)} "
            f"for mode 'recent' (got {recent_turns})",
        )
    return recent_turns


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _sanitize_attribute(value: object) -> str:
    """属性值单行化：引号/换行/制表符换成安全字符（块结构不许被正文破坏）。"""

    text = "" if value is None else str(value)
    for unsafe in _ATTRIBUTE_UNSAFE:
        text = text.replace(unsafe, " " if unsafe != '"' else "'")
    return " ".join(text.split())


def _sanitize_line(value: object) -> str:
    """列表项单行化（只压空白，不动引号）。"""

    text = "" if value is None else str(value)
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    """字数上限内截断；被截断时以 ``…`` 结尾，**长度恰为 limit**。"""

    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _open_tag(
    *,
    reference_id: str,
    lane_id: str,
    kind: str,
    title: str,
    origin: str,
    updated_at: str,
    turn_count: int,
) -> str:
    # 逐字采用 02 §3.1 的属性集与顺序（id / lane / kind / title / origin /
    # updated / turns）。偏差：文档示例把开标签折成两行，这里渲染成**单行**
    # （确定性更好、token 更省）；属性名与顺序不变。
    return (
        f'<referenced_conversation id="{_sanitize_attribute(reference_id)}"'
        f' lane="{_sanitize_attribute(lane_id)}"'
        f' kind="{_sanitize_attribute(kind)}"'
        f' title="{_sanitize_attribute(title)}"'
        f' origin="{_sanitize_attribute(origin)}"'
        f' updated="{_sanitize_attribute(updated_at)}"'
        f' turns="{int(turn_count)}">'
    )


def _render(
    *,
    reference_id: str,
    lane_id: str,
    kind: str,
    title: str,
    origin: str,
    updated_at: str,
    turn_count: int,
    window: Sequence[TurnView],
    bullets: Sequence[str],
    artifacts: Sequence[str],
    user_cap: int,
    assistant_cap: int,
) -> str:
    lines: list[str] = [
        _open_tag(
            reference_id=reference_id,
            lane_id=lane_id,
            kind=kind,
            title=title,
            origin=origin,
            updated_at=updated_at,
            turn_count=turn_count,
        )
    ]
    if kind == EPHEMERAL_KIND:
        # B5（02 §5.3）：引用临时对话必须在块里标明来源 + 长期记忆的可能后果。
        lines.append(
            "[注意] 临时对话（来源："
            f"{_sanitize_line(origin) or '未知'}）。"
            "显式引用后其内容可能被确认为长期记忆。"
        )
    lines.append(f"[最近 {len(window)} 轮]")
    for turn in window:
        lines.append(f"  <user>{_clip(turn.user_content, user_cap)}</user>")
        lines.append(
            f"  <assistant>{_clip(turn.assistant_content, assistant_cap)}</assistant>"
        )
    lines.append("[更早的要点]")
    if bullets:
        lines.extend(
            f"- {_clip(_sanitize_line(bullet), EARLIER_BULLET_CHARS)}"
            for bullet in bullets
        )
    else:
        lines.append(EMPTY_MARKER)
    lines.append("[触及的产物/文件]")
    if artifacts:
        lines.extend(f"- {_sanitize_line(name)}" for name in artifacts)
    else:
        lines.append(EMPTY_MARKER)
    lines.append("</referenced_conversation>")
    return "\n".join(lines)


def project_reference(
    *,
    reference_id: str,
    title: str,
    kind: str,
    lane_id: str,
    updated_at: str,
    turn_count: int,
    turns: Sequence[TurnView],
    artifacts: Sequence[str],
    mode: str,
    recent_turns: Optional[int],
    max_tokens: int,
    origin: str = "",
) -> ReferenceProjection:
    """把一条被引用会话折成有界投影块。

    **纯函数**：同输入 ⇒ 同输出（逐字节）。``max_tokens`` 是这条引用的硬上限；
    调用方应先按档位取 ``min(MODE_TOKEN_LIMITS[mode], 单轮剩余预算)``。

    ``turn_count`` 渲染进块的 ``turns=`` 属性。**属性名是 turns（轮次），所以
    调用方必须喂轮次数**——复审 I-1 之前这里喂的是 entry 计数，3 轮的会话会渲染成
    ``turns="6"``（每轮 2 条 entry，有工具调用/notice 时更大）。
    """

    normalized_mode = normalize_mode(mode)
    if kind not in REFERENCE_KINDS:
        raise ReferenceProjectionError(
            "kind",
            f"unknown kind {kind!r}; expected one of {', '.join(REFERENCE_KINDS)}",
        )
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        raise ReferenceProjectionError("maxTokens", "must be an integer")
    if max_tokens < 1:
        raise ReferenceProjectionError("maxTokens", "must be positive")

    window_size = resolve_recent_turns(mode=normalized_mode, recent_turns=recent_turns)
    ordered_turns = tuple(turns)
    if normalized_mode == MODE_FULL or window_size is None:
        window_size = len(ordered_turns)
    window_size = max(0, min(window_size, len(ordered_turns)))

    # 更早的要点：窗口之外、**仅用户消息**、取距窗口最近的至多 8 条。
    earlier_turns = ordered_turns[: len(ordered_turns) - window_size]
    bullets: list[str] = [
        turn.user_content for turn in earlier_turns if turn.user_content.strip()
    ]
    bullets = bullets[-MAX_EARLIER_BULLETS:]
    artifact_names: list[str] = []
    for name in artifacts:
        cleaned = _sanitize_line(name)
        if cleaned and cleaned not in artifact_names:
            artifact_names.append(cleaned)

    user_cap = USER_TURN_CHARS
    assistant_cap = ASSISTANT_TURN_CHARS
    current_window = window_size
    truncated = False

    def render() -> str:
        return _render(
            reference_id=reference_id,
            lane_id=lane_id,
            kind=kind,
            title=title,
            origin=origin,
            updated_at=updated_at,
            turn_count=turn_count,
            window=ordered_turns[len(ordered_turns) - current_window :]
            if current_window
            else (),
            bullets=bullets,
            artifacts=artifact_names,
            user_cap=user_cap,
            assistant_cap=assistant_cap,
        )

    text = render()
    estimate = estimate_text_tokens(text)

    # 削减阶梯（固定顺序，确定性）：先掉最老的要点 → 再掉尾部产物名 →
    # 再缩窗口 → 再对半砍文本字数 → 最后硬截断。任何一步发生都置 truncated。
    while estimate > max_tokens:
        truncated = True
        if bullets:
            bullets.pop(0)
        elif artifact_names:
            artifact_names.pop()
        elif current_window > 1:
            current_window -= 1
        elif user_cap > MIN_TEXT_CAP or assistant_cap > MIN_TEXT_CAP:
            user_cap = max(MIN_TEXT_CAP, user_cap // 2)
            assistant_cap = max(MIN_TEXT_CAP, assistant_cap // 2)
        else:
            # 估算口径是 ceil(len/3)，故 max_tokens 个 token 恰好放下 3*max_tokens 字符。
            budget_chars = max_tokens * 3
            if budget_chars <= len(TRUNCATION_MARKER):
                text = text[:budget_chars]
            else:
                text = (
                    text[: budget_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
                )
            estimate = estimate_text_tokens(text)
            break
        text = render()
        estimate = estimate_text_tokens(text)

    return ReferenceProjection(
        text=text,
        token_estimate=estimate,
        truncated=truncated,
        mode=normalized_mode,
        max_tokens=max_tokens,
        rendered_turns=current_window,
        rendered_earlier_bullets=len(bullets),
        rendered_artifacts=len(artifact_names),
    )
