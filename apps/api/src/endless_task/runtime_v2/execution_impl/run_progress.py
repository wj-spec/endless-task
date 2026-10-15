"""E1：运行期**进度**读取（续跑的前提）——与"工作区快照 checkpoint"无关。

为什么需要它（批次 E / ADR §3.1）
================================

`AgentRunExecutor.execute(run_id)` 今天对同一个 run 再调一次就是**从头重做**：

* 它的上下文只来自 ``list_entry_context_entries(run.trigger_entry_id)``——触发器 entry
  的**祖先链**；而运行期自己写下的 TOOL_CALL / TOOL_RESULT / CONTEXT_SUMMARY 条目全是
  它的**后代**，一律不在内（`storage/v2/base.py` 的 ``_entry_context_rows`` 是
  ``WITH RECURSIVE`` 往**上**走）。
* ``v2_model_turns`` / ``v2_tool_executions`` 在 ``execution_impl/`` 里**从不用于跳过**
  （ADR 事实 5 的订正：只用于收尾报表），所以循环不知道"哪些轮已经做完了"。

本模块把"这个 run 已经做完了什么"从**既有表**推导出来（零迁移、零新持久化结构），
供循环决定"从哪继续"，从而支撑 ADR §3.2 的两条判据：

1. **不重复已完成的模型轮**（``RunProgress.completed_turns``）；
2. **不复用未确认的副作用**（``RunProgress.uncertain_tools``）。

命名避让（本仓已有的三套 checkpoint 全都不是执行进度）
=====================================================

* ``execution_env/checkpoint.py``：工作区**文件**快照（内容寻址 + 带守卫的还原）；
* ``context_engine/checkpoint.py``：**压缩摘要**；
* ``runtime_v2/run_checkpoint.py`` / ``run_reconcile.py`` / ``run_restore.py``：每个 run
  **一次**的工作区快照 + 启动对账还原（回答"崩溃的 run 对**文件**的改动要不要撤回"）。

本模块只说"**进度 / 续跑**"，不叫 checkpoint；也不裸叫 ``progress``——那是
``execution_impl/progress.py`` 的**无进展（no-progress）检测**（观察/影子用途）。

载体分工（ADR §3.1 的四件套，其中 ④ 是本批**行为验证**后的订正）
===============================================================

===========  ====================================================================
① entries    ``v2_transcript_entries``：消息序列（既有 ``ContextProjection`` 已具备）
② turns      ``v2_model_turns``：哪些轮已完成 + 用量
③ tools      ``v2_tool_executions``：工具状态 → "结果已确认" / "结果无法确认"
④ deltas     ``v2_runtime_events.model_text_delta``：**每一轮**的助手文本
===========  ====================================================================

⚠️ **④ 不是可选项**（这是对"能发给 provider 的消息序列今天已可从 entry 链重建"这条
读码结论的订正，证据是行为级的）：

* ``finalize_run`` 只在 run **收尾**时写**一条** ASSISTANT_MESSAGE
  （``storage/v2/runs.py``），内容是 ``content_parts`` 累加出来的**所有轮文本的拼接**，
  不是"每轮一条"；
* TOOL_CALL 条目的 payload 只有 ``callId`` / ``toolName`` / ``arguments``——**没有文本**，
  于是 ``ContextProjection`` 为它合成的 assistant 消息 ``content`` 恒为 ``""``；
* 而循环真正发出去的那条是 ``ProviderMessage(role="assistant",
  content=assistant_content, tool_calls=...)``（``execution_impl/coordinator.py``），
  ``assistant_content`` 非空时两者就不等。

逐轮文本只落在 ``model_text_delta`` 事件里（``execution_impl/turns.py`` 每片一个 delta，
``replay.py`` 就是这么累 ``partial_content`` 的）。不做 ④，续跑就会**把已完成轮对模型
说过的话悄悄丢掉**——一致性用例会红（见 ``tests/test_runtime_v2_run_progress.py``）。

本批**仍然不做**"保留被中断那一轮的半截流式文本"（ADR §3.1：未完成的轮就是重做）；
④ 在这里只用于**已完成轮**。

纯函数边界
==========

``read_run_progress`` / ``recover_turn_assistant_texts`` / ``rebuild_provider_messages``
都是**模块级自由函数**、输入显式、不读时钟、不做 I/O、不写库：
冻结面（``tests/test_storage_method_inventory.py``）只采集**类方法名 + 签名**，
模块级函数不在采集范围（``run_sweep.py`` / ``progress.py`` 的先例）。
只有 ``collect_run_progress`` 读库，它是"取值"而不是"判定"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

from ...runtime.provider import ProviderMessage
from ..domain import (
    ModelTurnRecord,
    ModelTurnStatus,
    RunStatus,
    RuntimeEventRecord,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
)
from ..projection import ContextProjection

#: **终态** run 状态：到了这三个之一，run 的进度就定格了，不再"可续"。
#: 与 ``storage/v2/base.py`` 的 ``ACTIVE_RUN_STATUSES`` 互补（那边是活动集合）。
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)

#: 崩溃收尾（``finalize_interrupted_run``）给被压平的子行打的 ``error_details.reason``。
#: 它把 ``{created, validating, waiting_approval, running}`` **四个**活动态压成同一个
#: ``FAILED`` + ``retryable=0`` + 同一句话 ⇒ 行层面"未调用"与"已调用但结果未知"
#: **此后不可区分**。这正是"默认按可能已产生副作用处理"的直接理由。
INTERRUPTED_REASON = "interrupted"

#: 崩溃时会被"压平"的四个工具执行状态（`storage/v2/base.py`）。
_ACTIVE_TOOL_EXECUTION_STATUSES = frozenset(
    {
        ToolExecutionStatus.CREATED,
        ToolExecutionStatus.VALIDATING,
        ToolExecutionStatus.WAITING_APPROVAL,
        ToolExecutionStatus.RUNNING,
    }
)

#: 消息重建时**要带上的**运行期条目类型：模型可见的工具配对（TOOL_CALL 由投影按需
#: 合成）+ 压缩摘要（它携带 ``coveredEntryIds``，投影会据此跳过被覆盖的原文）。
_RUN_MESSAGE_ENTRY_TYPES = frozenset(
    {
        "tool_call",
        "tool_result",
        "context_summary",
    }
)


@dataclass(frozen=True)
class ToolProgress:
    """单个工具执行的进度视图。"""

    execution_id: str
    model_turn_id: str
    turn_index: int
    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    result_entry_id: Optional[str] = None
    error_code: Optional[str] = None
    retryable: Optional[bool] = None
    #: 结果**不确定**：崩溃时它处于活动态（可能已产生副作用，也可能一次都没调用）。
    uncertain: bool = False

    @property
    def confirmed(self) -> bool:
        """结果已确认（工具真的跑完并落了结果条目）。"""
        return (
            self.status is ToolExecutionStatus.COMPLETED
            and self.result_entry_id is not None
        )


@dataclass(frozen=True)
class TurnProgress:
    """单个模型轮的进度视图。``assistant_text`` 来自 ④（``model_text_delta``）。

    ⚠️ ``assistant_text`` **仅对 completed 轮有意义**（``ModelTurnStatus.COMPLETED``）：
    未完成的轮（崩溃时在飞的那一轮）也会带着"半截流式文本"，那是**被中断轮**的
    残片——ADR §3.1 的决策是"未完成的轮就是重做"，**不得**把它当成该轮的产出去
    重建消息序列或写进 run 正文。两个消费点都只走 ``completed_turns``
    （``rebuild_provider_messages`` 与 ``runner.execute`` 的内容拼装）⇒ 行为上不泄漏；
    这里点名是为了让后来者**不要**直接拿 ``turn.assistant_text`` 用。
    """

    turn_id: str
    turn_index: int
    status: ModelTurnStatus
    #: ④ 累出来的逐轮助手文本。**仅对 completed 轮有意义**（见类 docstring）：
    #: 未完成轮这里是"半截流式文本"，不是那一轮的产出，消费前必须先看 ``completed``。
    assistant_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_ids: tuple[str, ...] = ()
    tool_execution_ids: tuple[str, ...] = ()
    confirmed_tool_execution_ids: tuple[str, ...] = ()
    uncertain_tool_execution_ids: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return self.status is ModelTurnStatus.COMPLETED

    @property
    def uncertain(self) -> bool:
        return bool(self.uncertain_tool_execution_ids)


@dataclass(frozen=True)
class RunProgress:
    """一个 run 的进度快照（纯派生，不落任何新结构）。"""

    run_id: str
    turns: tuple[TurnProgress, ...] = ()
    tools: tuple[ToolProgress, ...] = ()
    #: run 自己的状态（``collect_run_progress`` 读库时填；纯函数调用方可不给）。
    #: 只有 ``resumable`` 用它——终态 run 不可续（见 ``resumable``）。
    run_status: Optional[RunStatus] = None

    @property
    def completed_turns(self) -> tuple[TurnProgress, ...]:
        return tuple(turn for turn in self.turns if turn.completed)

    @property
    def completed_turn_count(self) -> int:
        return len(self.completed_turns)

    @property
    def completed_turn_ids(self) -> tuple[str, ...]:
        return tuple(turn.turn_id for turn in self.completed_turns)

    @property
    def next_turn_index(self) -> int:
        """下一个**新**轮的 ``turn_index``（``start_model_turn`` 内部按 MAX+1 现算）。"""
        return max((turn.turn_index for turn in self.turns), default=0) + 1

    @property
    def input_tokens(self) -> int:
        return sum(turn.input_tokens for turn in self.completed_turns)

    @property
    def output_tokens(self) -> int:
        return sum(turn.output_tokens for turn in self.completed_turns)

    @property
    def produced_characters(self) -> int:
        return sum(len(turn.assistant_text) for turn in self.completed_turns)

    @property
    def tool_call_count(self) -> int:
        return sum(len(turn.tool_call_ids) for turn in self.completed_turns)

    @property
    def confirmed_tools(self) -> tuple[ToolProgress, ...]:
        return tuple(tool for tool in self.tools if tool.confirmed)

    @property
    def uncertain_tools(self) -> tuple[ToolProgress, ...]:
        """结果**无法确认**的工具：崩溃时处于活动态的那批（ADR §3.2 的判据 2）。"""
        return tuple(tool for tool in self.tools if tool.uncertain)

    @property
    def resumable(self) -> bool:
        """这个 run 能不能被续跑：**有已完成的轮** 且 **自己不是终态**。

        两条都必要：

        * 没有已完成轮（=今天绝大多数路径）就是一条全新路径，行为必须与今天逐字相同；
        * 终态 run（COMPLETED / FAILED / CANCELLED）**永远不可续**。E2 收尾后就是这种
          形态：run 记 COMPLETED，但收尾前在飞的那个 model turn 与"结果无法确认"的
          tool 行**故意留在活跃态**（见 ``execution_impl/runner.py`` 的 E2 收尾说明）。
          那两行是"可能已产生副作用"的证据，不是"还有活可干"的邀请——终态 run 自称
          ``resumable=True`` 会让读取器（E3 的续跑判据）对一条已经定案的 run 说"可以
          接着跑"，所以这里按 run 状态一刀切掉。
        """
        if self.run_status in _TERMINAL_RUN_STATUSES:
            return False
        return self.completed_turn_count > 0


def is_uncertain_tool_execution(record: ToolExecutionRecord) -> bool:
    """该工具执行的**结果是否无法确认**（可能已产生副作用）。

    两种形态都要认（这是"崩溃前"与"崩溃后"两个时刻的同一件事）：

    1. **崩溃前**（进程内，行仍活动）：状态落在 ``{created, validating,
       waiting_approval, running}``。注意 ``coordinator`` 在**批量执行之前**就把整批工具
       一律置 ``RUNNING``，所以 ``running`` **不等于**"真被调用过"——这正是必须
       "默认按可能已产生副作用处理"的原因（不能假定它没跑）。
    2. **崩溃后**（``finalize_interrupted_run`` 已把四个状态压平）：``FAILED`` +
       ``retryable is False`` + ``error_details.reason == "interrupted"``。此时行上
       已经**不可区分**"未调用"与"已调用但结果未知"，只能一并当成不确定。
    """
    if record.status in _ACTIVE_TOOL_EXECUTION_STATUSES:
        return True
    details = record.error_details
    if isinstance(details, Mapping) and details.get("reason") == INTERRUPTED_REASON:
        return record.retryable is not True
    return False


def recover_turn_assistant_texts(
    events: Sequence[RuntimeEventRecord],
) -> dict[str, str]:
    """按 ``model_turn_id`` 累加 ``model_text_delta`` 的 delta（载体 ④）。

    与 ``replay.py`` 累 ``partial_content`` 的姿势逐字一致：**只看有
    ``model_turn_id`` 的事件**，无 id 的 delta（例如更早的形态）不归属任何轮，
    不能拿来冒充某一轮的文本。
    """
    texts: dict[str, str] = {}
    for event in events:
        if event.event_type != "model_text_delta":
            continue
        turn_id = event.model_turn_id
        if not turn_id:
            continue
        delta = event.payload.get("delta")
        if not isinstance(delta, str) or not delta:
            continue
        texts[turn_id] = texts.get(turn_id, "") + delta
    return texts


def read_run_progress(
    *,
    run_id: str,
    model_turns: Sequence[ModelTurnRecord],
    tool_executions_by_turn: Mapping[str, Sequence[ToolExecutionRecord]],
    events: Sequence[RuntimeEventRecord] = (),
    run_status: Optional[RunStatus] = None,
) -> RunProgress:
    """纯函数：把既有行推导成进度视图（不读库、不看时钟、无副作用）。

    ``model_turns`` 按 ``turn_index`` 升序（``list_model_turns`` 的返回顺序），
    ``tool_executions_by_turn`` 以 model turn id 为键——工具执行是**按 model turn
    挂**的（``repository.list_tool_executions(turn.id)``），不要按 run 拼 SQL。

    ``run_status`` 只影响 ``resumable``（终态 run 不可续，见 ``RunProgress.resumable``）；
    不给就当"不知道 run 状态"，退回"有已完成轮即可续"的老口径。
    """
    texts = recover_turn_assistant_texts(events)
    turns: list[TurnProgress] = []
    tools: list[ToolProgress] = []
    for turn in model_turns:
        records = tuple(tool_executions_by_turn.get(turn.id, ()))
        tool_views = tuple(
            ToolProgress(
                execution_id=record.id,
                model_turn_id=record.model_turn_id,
                turn_index=turn.turn_index,
                call_id=record.call_id,
                tool_name=record.tool_name,
                status=record.status,
                result_entry_id=record.result_entry_id,
                error_code=record.error_code,
                retryable=record.retryable,
                uncertain=is_uncertain_tool_execution(record),
            )
            for record in records
        )
        tools.extend(tool_views)
        turns.append(
            TurnProgress(
                turn_id=turn.id,
                turn_index=turn.turn_index,
                status=turn.status,
                assistant_text=texts.get(turn.id, ""),
                input_tokens=turn.input_tokens or 0,
                output_tokens=turn.output_tokens or 0,
                tool_call_ids=tuple(
                    record.call_id for record in records if record.call_id
                ),
                tool_execution_ids=tuple(record.id for record in records),
                confirmed_tool_execution_ids=tuple(
                    view.execution_id for view in tool_views if view.confirmed
                ),
                uncertain_tool_execution_ids=tuple(
                    view.execution_id for view in tool_views if view.uncertain
                ),
            )
        )
    return RunProgress(
        run_id=run_id,
        turns=tuple(turns),
        tools=tuple(tools),
        run_status=run_status,
    )


def collect_run_progress(*, repository: Any, run_id: str) -> RunProgress:
    """读库版：把 ``read_run_progress`` 需要的四类行取齐（唯一的 I/O 入口）。

    姿势照 ``execution_impl/progress.py``：
    ``list_model_turns(run_id)`` → 逐轮 ``list_tool_executions(turn.id)`` →
    ``list_runtime_events(run_id)``；再加一条 ``get_run(run_id)`` 取 run 自己的状态
    （只喂 ``resumable``：终态 run 不可续，见 ``RunProgress.resumable``）。
    """
    model_turns = repository.list_model_turns(run_id)
    tool_executions_by_turn = {
        turn.id: repository.list_tool_executions(turn.id) for turn in model_turns
    }
    events = repository.list_runtime_events(run_id)
    run_status = repository.get_run(run_id).status
    return read_run_progress(
        run_id=run_id,
        model_turns=model_turns,
        tool_executions_by_turn=tool_executions_by_turn,
        events=events,
        run_status=run_status,
    )


def run_message_entries(
    entries: Sequence[TranscriptEntryRecord],
    *,
    run_id: str,
) -> tuple[TranscriptEntryRecord, ...]:
    """取"这个 run 亲手写下的、模型可见的"条目，按 ``seq`` 升序。

    两点刻意：

    * **按 run id 过滤**（``source_run_id``），不依赖"lane 链自动继承"——ADR §7 的
      硬约束：同 ``sibling_group_id`` 的续跑会让前任 run 的条目被判成"非活动变体"而
      从上下文里静默剔除（``include_variants`` 默认 False，评审 M5/I4）。按 run id
      直取就绕开了变体过滤：**该进来的条目一定会进来**。
    * 只取 ``tool_call`` / ``tool_result`` / ``context_summary``：助手文本由
      ``finalize_run`` 在**收尾**时写成一条 entry（内容是全轮拼接），它不是"某一轮的
      文本"，混进来只会污染重建；逐轮文本走 ④。
    """
    wanted = _RUN_MESSAGE_ENTRY_TYPES
    selected = (
        entry
        for entry in entries
        if entry.source_run_id == run_id and str(entry.type.value) in wanted
    )
    return tuple(sorted(selected, key=lambda entry: entry.seq))


def rebuild_provider_messages(
    *,
    entries: Sequence[TranscriptEntryRecord],
    progress: RunProgress,
    prefix_messages: Sequence[ProviderMessage] = (),
    projection: Optional[ContextProjection] = None,
) -> tuple[ProviderMessage, ...]:
    """重建"崩溃前已经**发出**的消息序列"（一致性用例的被测对象）。

    ``entries`` 必须是**投影输入本身**（祖先链 + ``run_message_entries``），这样
    ``ContextProjection`` 的既有语义全部生效：``context_summary`` 的
    ``coveredEntryIds`` 跳过、TOOL_CALL ↔ TOOL_RESULT 配对合成 assistant 消息、
    ``include_in_llm`` / ``transform`` 策略过滤。

    最后一步是 ④ 的落点：投影为 TOOL_CALL 合成的 assistant 消息 ``content`` 恒为
    ``""``，而实际发出的是该轮的助手文本；按 ``callId → 轮`` 把文本贴回去。
    """
    projector = projection or ContextProjection()
    messages = list(projector.project(entries).messages)
    if not messages:
        return tuple(prefix_messages)

    text_by_call_id: dict[str, str] = {}
    for turn in progress.completed_turns:
        if not turn.assistant_text:
            continue
        for call_id in turn.tool_call_ids:
            text_by_call_id.setdefault(call_id, turn.assistant_text)

    if not text_by_call_id:
        return tuple(prefix_messages) + tuple(messages)

    rebuilt: list[ProviderMessage] = []
    for message in messages:
        call_ids = tuple(call.id for call in (message.tool_calls or ()))
        text = next(
            (text_by_call_id[call_id] for call_id in call_ids if call_id in text_by_call_id),
            None,
        )
        if message.role == "assistant" and text is not None:
            rebuilt.append(replace(message, content=text))
        else:
            rebuilt.append(message)
    return tuple(prefix_messages) + tuple(rebuilt)
