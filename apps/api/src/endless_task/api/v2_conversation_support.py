"""v2 会话支撑（从 app.py 搬出，供 v2 消息/运行路由共用）。

- `resolve_runtime_v2_conversation`：v2 只有一套运行时，会话 id 自成树（历史迁移映射表已随迁移机制移除）。
- `title_conversation_from_first_message`：首条用户消息给会话命名（v1 时代行为的 v2 补齐）。
"""

from __future__ import annotations

from .container import AppContainer


def resolve_runtime_v2_conversation(
    container: AppContainer,
    conversation_id: str,
    *,
    write: bool,
) -> str:
    del write
    # 14 B2: v2 sole runtime — conversation id is its own tree (migration
    # mapping tables removed with the migration machinery).
    return conversation_id


def title_conversation_from_first_message(
    container, conversation_id: str, content: str
) -> None:
    """用首条用户消息给会话命名（v1 时代的行为，v2 路径补齐）。

    为什么需要：v1 在 `create_turn` 里会自动命名，v2 消息路径不经过那里，
    于是会话永远叫「新对话」——侧栏里一堆同名条目（用户反馈"摘要设计丢了"）。

    规则与 v1 一致（前 30 字、空白折叠）；手动命名过的标题不动；非首条消息
    不动。命名属于侧信道，失败不影响消息发送。
    """
    try:
        conversation = container.chat_repository.get_conversation(conversation_id)
        if conversation.title_is_manual:
            return
        if container.runtime_v2_repository.has_user_message(conversation_id):
            return
        container.chat_repository.set_automatic_title(conversation_id, content)
    except Exception:  # noqa: BLE001 命名失败不该阻塞发消息
        pass
