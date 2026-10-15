"""@ 引用的**校验与组装**（只读：在任何写入之前跑完）。

- 校验口径全部在这里（不存在 / 不可读 / 自引用 / 跨工作区 / 档位 / 窗口），
  失败一律抛**指名**的 :class:`ReferenceRequestError`，路由层翻成 4xx。
- 组装用既成事实：轮次选取复用 ``runtime_v2.compaction.history_turns``
  （压缩器同一份口径），token 估算复用 ``mentions.projection`` 里的既有启发式。
- **不写任何东西**；写入与补偿在 ``mentions.injection``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from endless_task.domain.models import Conversation, ConversationKind, ConversationStatus
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime_v2.compaction import history_turns
from endless_task.runtime_v2.domain import LaneKind, LaneStatus, RunStatus

from .projection import (
    DEFAULT_REFERENCE_COUNT,
    DEFAULT_TOTAL_TOKEN_BUDGET,
    MIN_REFERENCE_TOKENS,
    MODE_FULL,
    MODE_TOKEN_LIMITS,
    ReferenceProjection,
    ReferenceProjectionError,
    TurnView,
    normalize_mode,
    project_reference,
    resolve_recent_turns,
)

#: 与提交闸（``create_message_submission``）**同一组**活跃状态——这是本模块
#: 预检"同一语义"的依据；口径改一处必须两处一起改。
ACTIVE_RUN_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.CREATED,
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL,
    RunStatus.COMPACTING,
    RunStatus.CANCELLING,
)

KIND_EPHEMERAL = "ephemeral"
KIND_BRANCH = "branch"
KIND_NORMAL = "normal"


class ReferenceRequestError(RuntimeError):
    """@ 引用请求非法。``code`` 与 ``field`` 都要出现在 4xx 响应里（"指名"）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        field: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field


@dataclass(frozen=True)
class ConversationReference:
    """协议层的 ``references`` 项（判别联合 ``kind="conversation"``）。

    ``mode`` **没有默认值**（复审 M-4）：协议默认档 ``"summary"`` 的权威在
    ``api/schemas/v2_conversations.py`` 的 pydantic 字段上（路由永远经它传值），
    在这儿再放一个不可达的默认值只会让两处将来漂移——所以这里要求显式传入。
    """

    conversation_id: str
    mode: str
    lane_id: Optional[str] = None
    recent_turns: Optional[int] = None
    entry_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceTarget:
    """校验通过、可直接投影的被引用对象（全量读一次的快照）。"""

    conversation_id: str
    lane_id: str
    kind: str
    title: str
    origin: str
    updated_at: str
    entry_count: int
    turns: tuple[TurnView, ...]
    artifacts: tuple[str, ...]


@dataclass(frozen=True)
class ReferencePlanItem:
    target: ReferenceTarget
    projection: ReferenceProjection
    provenance: dict[str, object]

    @property
    def text(self) -> str:
        return self.projection.text


@dataclass(frozen=True)
class ReferencePlan:
    items: tuple[ReferencePlanItem, ...]
    warnings: tuple[str, ...]
    total_token_estimate: int


# ---------------------------------------------------------------------------
# 预检（只读）
# ---------------------------------------------------------------------------


def conversation_has_active_run(container, conversation_id: str) -> bool:
    """本次提交的会话是否有活跃 v2 run——**与提交闸同一语义**。

    为什么不用 ``gateway.has_active_run``：它只看**本进程**的在飞句柄，看不到
    上一次进程留下的 ``created``/``running`` 行；而 ``create_message_submission``
    拒绝的是**库里状态**在 :data:`ACTIVE_RUN_STATUSES` 里的 run。预检的意义正是
    "把可预见的失败挡在第一次写入之前"，所以这里必须查库。

    为什么不用 ``gateway.snapshot()['activeRunId']``：那个字段是"该 lane 最后一个
    ``is_active_variant`` 的 run"，**与状态无关**——一个**已终态**的 run 也会让它非空，
    会把今天能正常发送的消息误判成 409（证据：``runtime_v2/replay.py:333-350``，
    真正按状态过滤的是同一快照里的 ``runningRunId``）。口径错了会让功能变砖。
    """

    runs = container.runtime_v2_repository.list_runs(
        conversation_id=conversation_id,
        statuses=ACTIVE_RUN_STATUSES,
    )
    return bool(runs)


def referenced_conversation_has_active_run(container, conversation_id: str) -> bool:
    """被引用会话是否有在飞 run（**允许**，只记进 provenance/告警）。"""

    return conversation_has_active_run(container, conversation_id)


def resolve_reference_target(
    container,
    *,
    sending_conversation_id: str,
    reference: ConversationReference,
) -> ReferenceTarget:
    """把一条引用解析成可投影的快照；任何不合法都在**写入之前**抛指名错误。"""

    try:
        mode = normalize_mode(reference.mode)
    except ReferenceProjectionError as error:
        raise ReferenceRequestError(
            "mention_invalid_mode", str(error), field=error.field
        ) from error
    try:
        resolve_recent_turns(mode=mode, recent_turns=reference.recent_turns)
    except ReferenceProjectionError as error:
        raise ReferenceRequestError(
            "mention_invalid_recent_turns", str(error), field=error.field
        ) from error
    if reference.entry_ids:
        # 02 §3.2：一期**不做**选中轮次，只预留协议字段。非空即显式拒绝，
        # 不做静默忽略（静默忽略会让用户以为"我选中的那几轮被带过去了"）。
        raise ReferenceRequestError(
            "mention_entry_ids_unsupported",
            "entryIds 是一期预留字段，暂不支持按轮次选择；请留空数组。",
            field="entryIds",
        )

    conversation = _load_conversation(container, reference.conversation_id)
    if conversation.id == sending_conversation_id:
        raise ReferenceRequestError(
            "mention_self_reference",
            "不能把当前会话引用给它自己（本次发送的上下文里已经有它）。",
            field="conversationId",
        )
    _require_same_workspace(
        container,
        sending_conversation_id=sending_conversation_id,
        referenced=conversation,
    )
    lane = _resolve_lane(container, conversation, reference.lane_id)
    entries = container.runtime_v2_repository.list_lane_context_entries(lane.id)
    turns = tuple(
        TurnView(
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
        )
        for turn in history_turns(entries, boundary_run_id=None)
    )
    kind = _reference_kind(conversation, lane)
    return ReferenceTarget(
        conversation_id=conversation.id,
        lane_id=lane.id,
        kind=kind,
        title=_reference_title(conversation, lane, kind),
        origin=_reference_origin(container, conversation, kind),
        updated_at=conversation.updated_at,
        entry_count=len(entries),
        turns=turns,
        artifacts=_artifact_names(container, conversation.id),
    )


def _load_conversation(container, conversation_id: str) -> Conversation:
    try:
        conversation = container.chat_repository.get_conversation(conversation_id)
    except NotFoundError as error:
        raise ReferenceRequestError(
            "mention_conversation_not_found",
            f"被引用的会话不存在：{conversation_id}",
            status_code=404,
            field="conversationId",
        ) from error
    if conversation.status is ConversationStatus.ARCHIVED:
        # "不可读"的实操含义：归档会话在 UI 上已不可进入，引用它会把用户
        # 看不到的正文注入上下文——按找不到处理，避免静默泄露。
        raise ReferenceRequestError(
            "mention_conversation_unreadable",
            f"被引用的会话已归档，不可引用：{conversation_id}",
            status_code=404,
            field="conversationId",
        )
    return conversation


def _require_same_workspace(
    container,
    *,
    sending_conversation_id: str,
    referenced: Conversation,
) -> None:
    """默认只允许**同工作区**（01 §5.2 ⑤：跨区会把别的目录的路径当成当前目录的）。"""

    sending = container.chat_repository.get_conversation(sending_conversation_id)
    if sending.workspace_id == referenced.workspace_id:
        return
    raise ReferenceRequestError(
        "mention_cross_workspace",
        "被引用的会话属于另一个工作区：它的文件路径不属于当前工作区，"
        "模型会把它们当成当前目录的路径去调用工具。默认不允许跨工作区引用。",
        field="conversationId",
    )


def _resolve_lane(container, conversation: Conversation, lane_id: Optional[str]):
    repository = container.runtime_v2_repository
    pointer = repository.get_conversation_pointer(conversation.id)
    if pointer is None:
        raise ReferenceRequestError(
            "mention_conversation_has_no_lane",
            f"被引用的会话还没有 v2 lane：{conversation.id}",
            field="laneId",
        )
    selected = lane_id or pointer.active_lane_id
    try:
        lane = repository.get_lane(selected)
    except NotFoundError as error:
        raise ReferenceRequestError(
            "mention_lane_not_found",
            f"被引用的 lane 不存在：{selected}",
            status_code=404,
            field="laneId",
        ) from error
    if lane.conversation_id != conversation.id:
        raise ReferenceRequestError(
            "mention_lane_not_in_conversation",
            f"lane {selected} 不属于会话 {conversation.id}",
            field="laneId",
        )
    if lane.status is LaneStatus.ARCHIVED or lane.kind is LaneKind.ARCHIVED:
        raise ReferenceRequestError(
            "mention_lane_unreadable",
            f"被引用的 lane 已归档，不可引用：{selected}",
            status_code=404,
            field="laneId",
        )
    return lane


def _reference_kind(conversation: Conversation, lane) -> str:
    if lane.kind is LaneKind.PERSISTENT_BRANCH:
        return KIND_BRANCH
    if conversation.kind is ConversationKind.EPHEMERAL:
        return KIND_EPHEMERAL
    return KIND_NORMAL


def _reference_title(conversation: Conversation, lane, kind: str) -> str:
    if kind == KIND_BRANCH and lane.display_name:
        return str(lane.display_name)
    return conversation.title


def _reference_origin(container, conversation: Conversation, kind: str) -> str:
    if kind == KIND_EPHEMERAL:
        source_title = _temporary_source_title(container, conversation.id)
        if source_title:
            return f"临时 · 源自《{source_title}》"
        return "临时对话"
    if kind == KIND_BRANCH:
        return f"分支 · 源自《{conversation.title}》"
    return ""


def _temporary_source_title(container, conversation_id: str) -> Optional[str]:
    try:
        record = container.runtime_v2_repository.get_temporary_conversation(
            conversation_id
        )
    except NotFoundError:
        return None
    if not record.source_conversation_id:
        return None
    try:
        source = container.chat_repository.get_conversation(
            record.source_conversation_id
        )
    except NotFoundError:
        return None
    return source.title


def _artifact_names(container, conversation_id: str) -> tuple[str, ...]:
    """``[触及的产物/文件]``：只列名（02 §3.1），不读内容、不读磁盘。

    只吞**预期内**的"这个会话没有产物"（``NotFoundError``，复审 M-5）。产物仓储
    真坏掉（数据库故障等）时**上抛**，不许静默渲染成"（无）"——那会让用户以为
    "这个会话没有产物"，而实际是读失败。
    """

    repository = container.artifact_repository
    try:
        records = repository.list_artifacts_for_conversation(conversation_id)
    except NotFoundError:
        return ()
    return tuple(str(record.title) for record in records if record.title)


# ---------------------------------------------------------------------------
# 组装（分配单条预算 → 纯函数投影）
# ---------------------------------------------------------------------------


def build_reference_plan(
    container,
    *,
    sending_conversation_id: str,
    references: Sequence[ConversationReference],
    total_budget: int = DEFAULT_TOTAL_TOKEN_BUDGET,
    full_mode_enabled: bool = False,
) -> ReferencePlan:
    """**先全部校验，再全部投影**——任何一条不合法都不产生半成品。

    预算：按请求顺序分配，单条取 ``min(档位上限, 单轮剩余预算)``；预算用尽
    之后的引用**不注入**并给告警（不许静默丢）。
    """

    if not references:
        return ReferencePlan(items=(), warnings=(), total_token_estimate=0)

    mode = None
    for reference in references:
        try:
            mode = normalize_mode(reference.mode)
            resolve_recent_turns(mode=mode, recent_turns=reference.recent_turns)
        except ReferenceProjectionError as error:
            raise ReferenceRequestError(
                "mention_invalid_reference", str(error), field=error.field
            ) from error
        if mode == MODE_FULL and not full_mode_enabled:
            raise ReferenceRequestError(
                "mention_full_mode_disabled",
                "整段引用（mode='full'）默认禁用，需要部署显式开启后再用。",
                field="mode",
            )

    targets = tuple(
        resolve_reference_target(
            container,
            sending_conversation_id=sending_conversation_id,
            reference=reference,
        )
        for reference in references
    )

    warnings: list[str] = []
    if len(targets) > DEFAULT_REFERENCE_COUNT:
        warnings.append(
            f"本次引用了 {len(targets)} 个会话，超过默认的 "
            f"{DEFAULT_REFERENCE_COUNT} 个：会显著增加上下文与每轮成本。"
        )

    items: list[ReferencePlanItem] = []
    used = 0
    for reference, target in zip(references, targets):
        resolved_mode = normalize_mode(reference.mode)
        window = resolve_recent_turns(
            mode=resolved_mode, recent_turns=reference.recent_turns
        )
        remaining = total_budget - used
        if remaining < MIN_REFERENCE_TOKENS:
            # 剩余预算不足以容纳一个有信息量的投影：**整条不注入**并告警，
            # 而不是塞一条几十 token 的残块（那会让人以为引用已生效）。
            warnings.append(
                f"单轮引用总量预算（{total_budget} token）已用尽"
                f"（剩余 {max(0, remaining)} token < {MIN_REFERENCE_TOKENS}），"
                f"会话 {target.conversation_id} 未被注入。"
            )
            continue
        max_tokens = min(MODE_TOKEN_LIMITS[resolved_mode], remaining)
        projection = project_reference(
            reference_id=target.conversation_id,
            title=target.title,
            kind=target.kind,
            lane_id=target.lane_id,
            updated_at=target.updated_at,
            turn_count=len(target.turns),
            turns=target.turns,
            artifacts=target.artifacts,
            mode=resolved_mode,
            recent_turns=window,
            max_tokens=max_tokens,
            origin=target.origin,
        )
        used += projection.token_estimate
        if projection.truncated:
            warnings.append(
                f"会话 {target.conversation_id} 的投影被截断"
                f"（上限 {max_tokens} token，实际 {projection.token_estimate}）。"
            )
        items.append(
            ReferencePlanItem(
                target=target,
                projection=projection,
                provenance={
                    # B3 要求的 provenance 字段（写进 context_policy 自由 JSON）。
                    "conversationId": target.conversation_id,
                    "laneId": target.lane_id,
                    "entryIds": list(reference.entry_ids),
                    "mode": resolved_mode,
                    "recentTurns": window,
                    "projectedCharacters": projection.character_count,
                    "tokenEstimate": projection.token_estimate,
                    "tokenBudget": max_tokens,
                    "truncated": projection.truncated,
                    "kind": target.kind,
                    "title": target.title,
                    "origin": target.origin,
                    "referencedLaneEntryCount": target.entry_count,
                    # 被引用会话此刻有在飞 run 也**允许**（读的是 append-only 的历史，
                    # 快照稳定）；记进 provenance 以便事后归因。
                    "referencedActiveRun": referenced_conversation_has_active_run(
                        container, target.conversation_id
                    ),
                },
            )
        )
    return ReferencePlan(
        items=tuple(items),
        warnings=tuple(warnings),
        total_token_estimate=used,
    )
