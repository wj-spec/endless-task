"""@ 引用（C3a）的 HTTP 粘合层：协议体 → ``mentions.service`` → 4xx 翻译 + 注入编排。

**顺序是硬要求**：

1. 解析发送 lane（与提交闸同一套检查）；
2. **校验全部引用**（不存在 / 不可读 / 自引用 / 跨工作区 / 档位 / 窗口）——全在写入前；
3. **活跃闸预检**（与提交闸同一语义，命中即按同一语义拒绝，**早于 append**）；
4. **幂等预检**（同一 ``Idempotency-Key`` 已用过 ⇒ 不写任何 entry，重放语义逐字不变）；
5. 逐条 append ``SYSTEM_NOTICE``（它自己推进 lane leaf ⇒ 提交时用户消息自动挂在它下面）；
6. 调既有提交路径（**签名零改动**）；失败 ⇒ 补偿；成功 ⇒ **后置核验**引用 entry
   真的在 ``trigger_entry_id`` 的祖先链上，否则补偿（孤儿不许留——补偿是**灭活**
   notice 而不是删行，见 ``mentions/injection.py``；两条补偿路径都**永不抛**，
   不许顶替已决定的 4xx / 2xx）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError
from endless_task.mentions.injection import (
    append_reference_notice,
    deactivate_reference_notices,
    message_request_exists,
    notice_is_ancestor_of,
)
from endless_task.mentions.service import (
    ConversationReference,
    ReferencePlan,
    ReferenceRequestError,
    build_reference_plan,
    conversation_has_active_run,
)
from endless_task.runtime_v2.domain import LaneKind, LaneRecord, LaneStatus

from .errors import ApiRequestError


logger = logging.getLogger(__name__)


@dataclass
class PreparedReferenceInjection:
    """已完成预检 + 已写入引用 entry（或有意未写）的一次提交准备。"""

    plan: ReferencePlan
    conversation_id: str
    lane_id: str
    notice_entry_ids: tuple[str, ...] = ()
    previous_leaf_entry_id: Optional[str] = None
    warnings: tuple[str, ...] = ()
    #: 幂等重放命中：本次**没有**写入任何 entry（重放语义与今天逐字相同）。
    replayed: bool = False
    compensated: list[str] = field(default_factory=list)
    #: 补偿/灭活自身失败（已记日志）。孤儿可能仍是 include_in_llm=True ⇒ 必须上报。
    compensation_failed: bool = False


def translate_reference_error(error: ReferenceRequestError) -> ApiRequestError:
    details = {"field": error.field} if error.field else None
    return ApiRequestError(
        error.code,
        error.message,
        status_code=error.status_code,
        details=details,
    )


def to_conversation_references(
    bodies: Sequence[object],
) -> tuple[ConversationReference, ...]:
    return tuple(
        ConversationReference(
            conversation_id=body.conversationId,
            lane_id=body.laneId,
            mode=body.mode,
            recent_turns=body.recentTurns,
            entry_ids=tuple(body.entryIds),
        )
        for body in bodies
    )


def build_plan(
    container,
    *,
    conversation_id: str,
    bodies: Sequence[object],
) -> ReferencePlan:
    try:
        return build_reference_plan(
            container,
            sending_conversation_id=conversation_id,
            references=to_conversation_references(bodies),
            full_mode_enabled=container.settings.mention_full_mode_enabled,
        )
    except ReferenceRequestError as error:
        raise translate_reference_error(error) from error


def resolve_submission_lane(
    container,
    *,
    conversation_id: str,
    lane_id: Optional[str],
) -> LaneRecord:
    """按 ``create_message_submission`` **同一套**检查解析本次发送的 lane。

    必须完全一致：引用 notice 要挂在用户消息将要去的那条 lane 上，否则它既不会
    成为触发器 entry 的祖先（静默失效），又会永久落在另一条 lane 上。

    唯一有意的差异：全新会话（还没有 v2 lane/pointer）**不允许**带引用发送——
    提交路径会在自己的事务里顺手建 main lane + pointer，而这里若抢着建，就又多出
    一次"提交前写入"（同样的不原子问题）。此时指名 400，不做静默忽略。
    """

    repository = container.runtime_v2_repository
    pointer = repository.get_conversation_pointer(conversation_id)
    if lane_id is None:
        if pointer is None:
            raise ApiRequestError(
                "mention_no_lane_to_anchor",
                "这个会话还没有 v2 lane（尚无任何消息）。请先发一条不带引用的消息，"
                "再使用 @ 引用。",
                status_code=400,
                details={"field": "laneId"},
            )
        selected_lane_id = pointer.active_lane_id
    else:
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        selected_lane_id = lane_id

    try:
        lane = repository.get_lane(selected_lane_id)
    except NotFoundError as error:
        raise NotFoundError(f"Lane not found: {selected_lane_id}") from error
    if lane.conversation_id != conversation_id:
        raise InvalidStateError("Lane does not belong to conversation")
    if lane.kind is LaneKind.ARCHIVED or lane.status is LaneStatus.ARCHIVED:
        raise ConflictError("Messages cannot be sent to an archived lane")
    return lane


def plan_injection(
    container,
    *,
    conversation_id: str,
    bodies: Sequence[object],
    lane_id: Optional[str],
) -> PreparedReferenceInjection:
    """**第一阶段：全部校验（零写入）**——解析发送 lane + 组装计划。

    放在路由里技能解析/自动命名之前，这样非法引用**连侧信道的写入都不会发生**。
    """

    lane = resolve_submission_lane(
        container, conversation_id=conversation_id, lane_id=lane_id
    )
    plan = build_plan(container, conversation_id=conversation_id, bodies=bodies)
    return PreparedReferenceInjection(
        plan=plan,
        conversation_id=conversation_id,
        lane_id=lane.id,
        previous_leaf_entry_id=lane.leaf_entry_id,
        warnings=plan.warnings,
    )


def write_injection(
    container,
    prepared: PreparedReferenceInjection,
    *,
    client_request_id: str,
) -> PreparedReferenceInjection:
    """**第二阶段：写入引用 entry**（在飞闸预检 + 幂等预检 → 再 append）。

    - 计划为空（校验过但预算用尽）⇒ 什么都不写，直接让提交继续（只是没有引用块）。
    - 预检命中 ⇒ 拒绝或跳过（不产生孤儿）。
    - append 中途失败 ⇒ 把已写下的补偿掉再抛（不留半条）。
    """

    if not prepared.plan.items:
        return prepared

    # B2 在飞闸预检：与提交闸同一语义、同一文案，但发生在**第一次写入之前**。
    if conversation_has_active_run(container, prepared.conversation_id):
        raise ConflictError("Conversation already has an active v2 run")

    # 幂等预检：重放时提交路径不会新建用户 entry，先写的引用 entry 必然成为孤儿。
    if message_request_exists(
        container.database,
        conversation_id=prepared.conversation_id,
        client_request_id=client_request_id,
    ):
        prepared.replayed = True
        return prepared

    repository = container.runtime_v2_repository
    notice_ids: list[str] = []
    try:
        for item in prepared.plan.items:
            record = append_reference_notice(
                repository,
                conversation_id=prepared.conversation_id,
                lane_id=prepared.lane_id,
                text=item.projection.text,
                provenance=item.provenance,
            )
            notice_ids.append(record.id)
    except BaseException:
        # append 中途失败：把已经写下的那些灭活掉，再**原样**抛出（补偿失败也不许
        # 顶替这个异常——C-1 的"永不抛"同时管这里）。
        try:
            deactivate_reference_notices(
                container.database,
                conversation_id=prepared.conversation_id,
                lane_id=prepared.lane_id,
                entry_ids=notice_ids,
                restore_leaf_entry_id=prepared.previous_leaf_entry_id,
                reason="partial_append_failed",
            )
        except Exception:  # noqa: BLE001 补偿失败只记日志，必须继续抛原始异常
            logger.exception("引用 notice 的部分补偿失败（可能残留 include_in_llm=True 的孤儿）")
        raise
    prepared.notice_entry_ids = tuple(notice_ids)
    return prepared


def compensate_injection(
    container,
    prepared: PreparedReferenceInjection,
    *,
    reason: str,
) -> int:
    """失败补偿：**灭活**刚写的引用 notice + 还原 lane leaf（B4「不留孤儿」）。

    **本函数永不抛**（复审 C-1 的硬要求）：补偿只是善后，绝不许顶替已经决定的响应
    ——提交失败路径的 **409 必须还是 409**、成功/幂等重放的 **202 不许变 500**。
    灭活失败时只记日志并置 :attr:`PreparedReferenceInjection.compensation_failed`
    （调用方据此回一条 warning），返回 0。
    """

    if not prepared.notice_entry_ids:
        return 0
    try:
        deactivated = deactivate_reference_notices(
            container.database,
            conversation_id=prepared.conversation_id,
            lane_id=prepared.lane_id,
            entry_ids=prepared.notice_entry_ids,
            restore_leaf_entry_id=prepared.previous_leaf_entry_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 补偿自身永不抛：只记日志，不改变已决定的响应
        prepared.compensation_failed = True
        logger.exception("引用 notice 补偿失败（孤儿可能仍会进入后续上下文）")
        return 0
    prepared.compensated.append(reason)
    return deactivated


def verify_injection_after_send(
    container,
    prepared: PreparedReferenceInjection,
    *,
    trigger_entry_id: str,
) -> tuple[str, ...]:
    """后置核验：引用 entry 必须在 ``trigger_entry_id`` 的**祖先链**上。

    这一条同时兜住两种"看似成功其实没注入"的情况：① 我们判断错了 lane；
    ② 幂等重放的竞态（预检通过之后另一个同 key 请求先完成）——此时提交路径
    返回的是**旧的**用户 entry，我们的 notice 不是它的祖先 ⇒ 是孤儿，必须补偿。

    **本函数永不抛**（复审 C-1）：核验或补偿自身失败时，返回一条如实的 warning，
    绝不许把已经成功的 **202 变成 500**。
    """

    if not prepared.notice_entry_ids:
        return ()
    try:
        if notice_is_ancestor_of(
            container.runtime_v2_repository,
            notice_entry_ids=prepared.notice_entry_ids,
            trigger_entry_id=trigger_entry_id,
        ):
            return ()
        deactivated = compensate_injection(
            container, prepared, reason="not_ancestor_of_trigger_entry"
        )
    except Exception:  # noqa: BLE001 后置核验自身也不许抛
        prepared.compensation_failed = True
        logger.exception("引用 notice 的后置祖先核验失败")
        return (
            "本轮 @ 引用未生效：核验引用块是否落在触发消息的祖先链上时出错"
            "（已记日志；引用块可能仍会进入后续上下文）。",
        )
    if prepared.compensation_failed:
        return (
            "本轮 @ 引用未生效：引用块没有落在触发消息的祖先链上，"
            "灭活引用记录时出错（已记日志；引用块可能仍会进入后续上下文）。",
        )
    return (
        "本轮 @ 引用未生效：引用块没有落在触发消息的祖先链上，"
        f"已灭活 {deactivated} 条引用记录"
        "（这些记录不会再进入之后任何一轮的上下文）。",
    )
