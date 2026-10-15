"""子运行终态回流父会话的唤醒服务（切片 9 M2）。

职责：子运行落终态后，把它的结果**合并成一条消息**投回父会话并起一轮，让父
agent 不必被用户追问才发现子代理跑完了。

为什么定义在委派层、靠注入回调：投递的唯一公共入口是网关的
`RuntimeV2SessionGateway.send`（校验会话 → 建提交 → 起一轮），而
`delegation/*` 不得 import `runtime_v2` / `storage` / `api`（架构门禁）。所以
本模块只认鸭子类型：`send` / `notify` / `conversation_is_active` 全由
`api/wiring.py` 注入。

定死的纪律（每条都对应一个测试）：

* **一批一条**：窗口内到达的终态合成一条消息。
* **幂等键按批内容派生**（`childwakeup:{sha256(排序后的 child_run_id 列表)[:16]}`）：
  同一批重投得到同一个键（仍是幂等的）；**不同的批必得不同的键**，哪怕两批共享
  同一条最早终态的行——库里某行的 `reported_at` 卡住不再会毒化整条通道。
  代价是"上一批已投递过的子运行又出现在这一批里"时会重发一次它的摘要，
  这可以接受，且严格优于永久丢消息。
* **投递失败不重试**：投递被拒最常见的原因是"会话已有活跃运行"，而那种情况下
  子运行会在下一次运行开始时的 `<delegated-children>` 注入里自然出现；再加重试
  机制只会制造循环。所以失败只降级成一条收件箱通知。（注意：这只是**消息**重投
  的禁令；投递成功后的盖章是幂等的，允许重试一次，见下条。）
* **盖章失败不静默**：投递成功后逐行盖 `reported_at`；写入失败重试一次，仍失败
  则除了 `logger.exception` 还要发一条显式通知——否则"投递成功但记录没盖上"这种
  状态只在日志里可见，而它会让同一批内容被反复重发。
* **可关**：`enabled=False` 时连库都不查。
* **重启后再武装**：`schedule` 只在终态那一刻被调用，而重启会丢掉内存态；启动时
  调一次 `rearm_unreported()` 把库里 `reported_at IS NULL` 的终态行重新排上，自动
  回流不必等用户先说话。它复用 `schedule`，不新写第二条投递路径。
* **每会话节流（只推迟不丢批）**：距本会话上次**成功投递**不足 `throttle_seconds`
  时，窗口先睡到边界再投。服务层默认 `0.0`（不节流），生产默认 60 秒由设置给。
* **归档只通知不起轮**：v2 发送路径只挡归档 *lane*，不挡归档 *会话*，所以会话
  是否活跃必须由注入回调自己判。
* **关停不干等**：窗口任务只是 `asyncio.sleep` + 投递，关停一律取消（与
  `delegation_handler.shutdown()` 同一纪律）；唤醒绝不能等一个长跑的子运行。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from typing import Awaitable, Callable, Dict, Optional, Sequence, Set

from endless_task.agent_platform import AgentPlatformError
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor

from .child_context import CHILD_STATE_LABELS, truncate_child_text
from .child_run_store import ChildRunRow

logger = logging.getLogger(__name__)

#: 合并窗口：子运行终态后等这么久，把同一会话窗口内到达的终态合成一条消息。
DEFAULT_MERGE_WINDOW_SECONDS = 2.0
#: 同时在飞的窗口上限（按会话数）。满员时新终态留在待投递集合里，等某个窗口
#: 收尾时再拣起来——上限是为了"永不风暴"，不是为了让终态丢失。
DEFAULT_MAX_INFLIGHT_CONVERSATIONS = 1
#: 每会话投递节流窗口（秒）：距本会话上次**真投递成功**不足这么久时，下一个窗口
#: 睡到边界再投（**只推迟、不丢批**）。**服务层默认 0.0 = 不节流**，生产默认由
#: 设置给 60 秒（`ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS`，`api/wiring.py`
#: 传进来）：服务层的既有单测与既有行为因此逐字不变，不会被隐藏的 sleep 挂住。
DEFAULT_THROTTLE_SECONDS = 0.0

#: 幂等键前缀（与 `taskrun:` / `runtime-v2-` 并列的命名法）。后缀不是任何单行
#: 的 id，而是**整批内容**的短摘要，见 :func:`_wakeup_key`。
WAKEUP_KEY_PREFIX = "childwakeup:"

#: 终态标签与 `<delegated-children>` 注入块同一套：**同一份定义**在
#: `child_context.CHILD_STATE_LABELS`，这里不再抄第二份（父 agent 两处看到同一组词）。


def render_wakeup_message(rows: Sequence[ChildRunRow]) -> str:
    """把一个批次的终态子运行渲染成一条回流消息（纯函数）。

    逐条：`childRunId | 状态 | 摘要（截断 200 字）`。摘要缺失时退回
    `safe_message`——失败原因不能只剩一句"已失败"。
    """
    if not rows:
        return ""
    lines = [f"子代理运行已结束（共 {len(rows)} 个），结果如下："]
    for row in rows:
        parts = [
            row.child_run_id,
            CHILD_STATE_LABELS.get(row.state, row.state.value),
        ]
        detail = _detail_text(row)
        if detail:
            parts.append(detail)
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _detail_text(row: ChildRunRow) -> str:
    if row.summary:
        text = row.summary
    elif row.safe_message:
        text = row.safe_message
    else:
        return ""
    return truncate_child_text(text)


def _wakeup_key(rows: Sequence[ChildRunRow]) -> str:
    """按**批内容**派生投递幂等键（纯函数）。

    取该批所有 `child_run_id` 排序后用 `,` 连接，`sha256` 摘要取前 16 位十六
    进制。为什么不能用"批内最早终态那行的 id"当键：那个设计只在"投递成功后整批
    都盖上了 `reported_at`"时才成立；只要锚点那一行的盖章失败，它就会永远留在
    "最早未汇报"的位置上，于是每一批都算出同一个键、却带着不断增长的正文，网关
    按"同键不同内容"拒收，该会话的自动唤醒就永久死掉。

    内容派生给出的三条性质（都有测试钉住）：

    1. 同一批重投（例如重试同一次窗口）算出同一个键，仍然幂等；
    2. 不同的批必得不同的键——即使两批共享同一条最早终态的行（卡住的
       `reported_at` 再也毒化不了通道），因为集合本身不同；
    3. 某一批里重复带上"以前投递过的子运行"时只是把它的摘要再发一次，
       可以接受，且严格优于把整个会话的唤醒永久丢掉。
    """
    digest = hashlib.sha256(
        ",".join(sorted(row.child_run_id for row in rows)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{WAKEUP_KEY_PREFIX}{digest}"


def _mark_failure_text(failed: Sequence[ChildRunRow], text: str) -> str:
    """盖章持续失败时的显式通知正文（中文）。"""
    child_ids = "、".join(row.child_run_id for row in failed)
    return (
        "唤醒消息已成功投递到本会话，但以下子运行的“已汇报”标记写入失败"
        "（已重试一次）："
        f"{child_ids}。这些结果会在下一次唤醒批次里再次出现，可能被重复投递；"
        "父会话仍已收到上面的合并结果。\n\n"
        f"{text}"
    )


class ChildWakeupService:
    """子运行终态 → 合并窗口 → 投递回父会话（进程级单例，由容器持有）。

    依赖全是注入的鸭子类型，因此本模块可以守住委派层的分层门禁：

    * ``store``：Task 1 的子运行存储面（`list_unreported_terminal_child_runs`
      / `mark_child_reported`）；
    * ``send(conversation_id, text, key)``：异步投递（生产上包的是网关 `send`）；
    * ``notify(conversation_id, text)``：同步降级通知（收件箱）；
    * ``conversation_is_active(conversation_id)``：会话是否还活跃（归档/已删）。
    """

    def __init__(
        self,
        *,
        store,
        send: Callable[[str, str, str], Awaitable[None]],
        notify: Callable[[str, str], None],
        conversation_is_active: Callable[[str], bool],
        enabled: bool,
        merge_window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS,
        max_inflight_conversations: int = DEFAULT_MAX_INFLIGHT_CONVERSATIONS,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        supervisor: Optional[object] = None,
    ) -> None:
        if not callable(
            getattr(store, "list_unreported_terminal_child_runs", None)
        ) or not callable(getattr(store, "mark_child_reported", None)):
            raise _invalid("唤醒服务需要能列出未汇报子运行并盖章的子运行存储")
        if not callable(send):
            raise _invalid("唤醒服务需要可调用的投递回调")
        if not callable(notify):
            raise _invalid("唤醒服务需要可调用的降级通知回调")
        if not callable(conversation_is_active):
            raise _invalid("唤醒服务需要可调用的会话活跃判定回调")
        window = float(merge_window_seconds)
        if (
            isinstance(merge_window_seconds, bool)
            or not math.isfinite(window)
            or window <= 0
        ):
            raise _invalid("合并窗口必须是有限正数")
        if (
            isinstance(max_inflight_conversations, bool)
            or not isinstance(max_inflight_conversations, int)
            or max_inflight_conversations <= 0
        ):
            raise _invalid("在飞窗口上限必须是正整数")
        throttle = float(throttle_seconds)
        # 与合并窗口同形，但**允许 0**：0 是"关掉节流"的正规表达（服务层默认就是
        # 它），负数/nan/inf/bool 则不是——inf 让节流变成"永远不投"，nan 绕过比较。
        if (
            isinstance(throttle_seconds, bool)
            or not math.isfinite(throttle)
            or throttle < 0
        ):
            raise _invalid("节流窗口必须是有限非负数")
        if supervisor is not None and not callable(getattr(supervisor, "spawn", None)):
            raise _invalid("supervisor 必须暴露 spawn(awaitable, *, name=...)")
        self._store = store
        self._send = send
        self._notify = notify
        self._conversation_is_active = conversation_is_active
        self._enabled = bool(enabled)
        self._merge_window_seconds = window
        self._max_inflight_conversations = max_inflight_conversations
        self._throttle_seconds = throttle
        # 不注入就自造一个进程级主管：窗口任务必须有人负责关停与记日志。
        self._supervisor = (
            supervisor
            if supervisor is not None
            else BackgroundTaskSupervisor(name="delegation-wakeup", logger=logger)
        )
        #: 会话 → 待投递的 child_run_id 集合。库里才是真相（窗口到点重新查库），
        #: 这里只回答"哪些会话还欠一个窗口"。
        self._pending: Dict[str, Set[str]] = {}
        #: 会话 → 在飞窗口任务。一个会话同时只有一个（在飞上限按会话计）。
        self._window_tasks: Dict[str, asyncio.Task] = {}
        #: 会话 → 上次**成功投递**的单调时钟时刻（节流边界据此算）。内存态：随进程
        #: 消亡，`shutdown` 不必清。只在真投递成功后写——降级通知与失败都不算一次
        #: "唤醒运行"，否则一次被拒的投递会白白吃掉下一批的节流额度。
        self._last_delivery: Dict[str, float] = {}
        self._closed = False

    # -- 调度面 -------------------------------------------------------------

    def schedule(self, child_run_id: str, parent_conversation_id: str) -> None:
        """登记一个刚终态的子运行（**同步、非阻塞**）。

        协调器在终态落库之后调用它；这里只登记并可能起一个窗口任务，绝不等待
        投递。关掉（`enabled=False`）时直接返回：不建任务、不投递、不通知。
        """
        if not self._enabled or self._closed:
            return
        conversation_id = (parent_conversation_id or "").strip()
        child_id = (child_run_id or "").strip()
        if not conversation_id or not child_id:
            # 没有父会话就无处回流（历史行 / 测试构造的行）：记一条调试日志即可。
            logger.debug(
                "子运行终态缺少父会话，唤醒已跳过",
                extra={"child_run_id": child_run_id},
            )
            return
        self._pending.setdefault(conversation_id, set()).add(child_id)
        self._ensure_window(conversation_id)

    def rearm_unreported(self) -> int:
        """启动时再武装：给"还有未汇报终态子运行"的会话各排一个唤醒窗口。

        `schedule` 只在**终态那一刻**被调用，而重启会丢掉 `_pending` 与在飞窗口；
        库里 `reported_at IS NULL` 的行于是要等用户先说话（下一轮运行的
        `<delegated-children>` 注入）才回得去。启动时补这一次，自动回流在重启后
        立刻恢复。复用 `schedule`，所以合并窗口/在飞上限/归档判定/失败降级
        全部同一条路径。返回登记了几个会话（日志与测试用）。
        """
        if not self._enabled or self._closed:
            return 0
        try:
            rows = self._store.list_unreported_terminal_child_runs()
        except Exception:  # noqa: BLE001 - 启动路径不能被唤醒侧挡住
            logger.exception("读取未汇报子运行失败，启动再武装放弃")
            return 0
        conversations: Set[str] = set()
        for row in rows:
            conversation_id = (row.parent_conversation_id or "").strip()
            child_run_id = (row.child_run_id or "").strip()
            if not conversation_id or not child_run_id:
                continue
            conversations.add(conversation_id)
            self.schedule(child_run_id, conversation_id)
        if conversations:
            logger.info(
                "启动再武装：为 %d 个会话排了唤醒窗口",
                len(conversations),
                extra={"conversation_count": len(conversations)},
            )
        return len(conversations)

    def _ensure_window(self, conversation_id: str) -> None:
        if self._closed:
            return
        if conversation_id in self._window_tasks:
            # 该会话已有在飞窗口：它的库查询会把新到的终态一并带上。
            return
        if len(self._window_tasks) >= self._max_inflight_conversations:
            # 全局在飞满了：留在待投递集合里，等某个窗口收尾时再起。
            return
        self._start_window(conversation_id)

    def _start_window(self, conversation_id: str) -> None:
        try:
            task = self._supervisor.spawn(
                self._run_window(conversation_id),
                name=f"delegation-wakeup:{conversation_id}",
            )
        except RuntimeError:
            # 主管已关闭（关停窗口）或当前没有事件循环：不投递、不标记。未汇报的
            # 行仍在库里，下一轮运行的注入块会带上它。
            logger.info(
                "唤醒窗口未启动（后台任务主管已关闭或没有事件循环）",
                extra={"conversation_id": conversation_id},
            )
            return
        self._window_tasks[conversation_id] = task
        task.add_done_callback(
            lambda _finished, conversation=conversation_id: self._on_window_done(
                conversation
            )
        )

    def _on_window_done(self, conversation_id: str) -> None:
        """窗口收尾：放掉在飞名额，并拣起窗口期间新到的终态。

        投递期间新到的终态不在本窗口那次库查询里（查询是投递**之前**做的），
        所以在飞上限若只是"拒绝新窗口"就会静默漏掉它们；收尾时按待投递集合
        补起下一个窗口，才是"上限防风暴"而不是"上限吃终态"。
        """
        self._window_tasks.pop(conversation_id, None)
        if self._closed:
            return
        for pending_conversation_id in tuple(self._pending):
            self._ensure_window(pending_conversation_id)

    async def drain(self) -> None:
        """等所有待投递的窗口都投完（测试用；生产关停走 :meth:`shutdown`）。

        先按待投递集合补窗口，再等在飞的：只等在飞会有"回调还没跑完就返回"的
        时序缝（`_on_window_done` 是 done 回调，不保证在 gather 返回前已经执行）。
        """
        while True:
            if self._closed:
                return
            for pending_conversation_id in tuple(self._pending):
                self._ensure_window(pending_conversation_id)
            running = tuple(
                task for task in self._window_tasks.values() if not task.done()
            )
            if not running:
                return
            await asyncio.gather(*running, return_exceptions=True)
            # 让 _on_window_done 这类 done 回调先跑完，再决定是否还有下一批。
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        """关停：取消在飞窗口并排空（与 `delegation_handler.shutdown()` 同一纪律）。

        **不能干等**：窗口虽然只睡合并窗口那么久，但它投递的那一步会碰网关，
        而进程退出不能被任何后台任务挂住。取消掉窗口等于放弃这次自动回流——
        未汇报的行仍在库里，下一次终态或下一轮运行的注入块会补上。
        """
        self._closed = True
        tasks = tuple(self._window_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._window_tasks.clear()
        self._pending.clear()

    # -- 投递 ---------------------------------------------------------------

    async def _run_window(self, conversation_id: str) -> None:
        await asyncio.sleep(self._merge_window_seconds)
        # 节流边界必须在**取批之前**等：等待期间新到的终态还留在 `_pending` 里，
        # 也还在这段 sleep 之后的同一次库查询里，于是自然并进这一批，而不是被
        # 推到"下一次投递"（那等于丢批）。
        await self._await_throttle_boundary(conversation_id)
        # 先"取走"本窗口负责的批次（同步一段，中间没有 await）：这之后新登记的
        # 终态算下一批，由 _on_window_done 起下一个窗口负责。
        self._pending.pop(conversation_id, None)
        try:
            rows = tuple(
                self._store.list_unreported_terminal_child_runs(
                    parent_conversation_id=conversation_id
                )
            )
        except Exception:  # noqa: BLE001 - 读不到就放弃本次唤醒（不重试、不通知）
            logger.exception(
                "读取未汇报子运行失败，本次唤醒放弃",
                extra={"conversation_id": conversation_id},
            )
            return
        if not rows:
            return
        text = render_wakeup_message(rows)
        try:
            active = bool(self._conversation_is_active(conversation_id))
        except NotFoundError:
            # 会话已不存在：通知指向一个不存在的会话没有意义；也不标记（无副作用）。
            logger.info(
                "父会话已不存在，唤醒投递放弃",
                extra={"conversation_id": conversation_id},
            )
            return
        except Exception:  # noqa: BLE001 - 判定是尽力而为，投递的会话校验才是墙
            logger.exception(
                "判定父会话是否活跃失败（按未归档继续）",
                extra={"conversation_id": conversation_id},
            )
            active = True
        if not active:
            # 归档会话：可以留通知，但绝不能给它起一轮。
            self._notify_safely(conversation_id, text)
            return
        try:
            await self._send(conversation_id, text, _wakeup_key(rows))
        except NotFoundError:
            # 会话已不存在：通知指向一个不存在的会话没有意义；也不标记（无副作用）。
            logger.info(
                "父会话已不存在，唤醒投递放弃",
                extra={"conversation_id": conversation_id},
            )
            return
        except Exception:  # noqa: BLE001 - 失败一律降级，**不重试、不标记**
            logger.exception(
                "唤醒投递失败，降级为收件箱通知（不重试）",
                extra={"conversation_id": conversation_id},
            )
            self._notify_safely(conversation_id, text)
            return
        self._last_delivery[conversation_id] = time.monotonic()
        self._mark_reported(conversation_id, rows, text)

    async def _await_throttle_boundary(self, conversation_id: str) -> None:
        """距本会话上次投递不足节流窗口时，睡到边界（不丢批，只推迟）。

        为什么是"推迟"而不是"丢了等下次"：下一次回流要等用户开口（下一轮运行的
        `<delegated-children>` 注入），丢掉这一批就等于"用户不问就永远看不到这条
        终态"。睡到边界的代价只是延迟，且睡期间本窗口仍占着在飞名额（在飞上限按
        会话计），不会因此叠出窗口风暴。
        """
        if self._throttle_seconds <= 0:
            return
        last = self._last_delivery.get(conversation_id)
        if last is None:
            return
        remaining = self._throttle_seconds - (time.monotonic() - last)
        if remaining > 0:
            logger.debug(
                "唤醒节流：本会话距上次投递不足窗口，推迟到边界",
                extra={"conversation_id": conversation_id, "remaining": remaining},
            )
            await asyncio.sleep(remaining)

    def _notify_safely(self, conversation_id: str, text: str) -> None:
        """降级通知；通知本身失败也只记日志——它已经是最后一道尽力而为。"""
        try:
            self._notify(conversation_id, text)
        except Exception:  # noqa: BLE001 - 不能让通知侧的故障冒泡成后台任务失败
            logger.exception(
                "写入唤醒降级通知失败",
                extra={"conversation_id": conversation_id},
            )

    def _mark_reported(
        self, conversation_id: str, rows: Sequence[ChildRunRow], text: str
    ) -> None:
        """投递成功后逐行盖 `reported_at`；失败重试一次，仍失败则显式通知。

        盖章与投递不同：`mark_child_reported` 是幂等的（把同一行再盖一次没有
        副作用），所以重试一次不属于 brief 禁止的"消息重投"。但"盖章失败"必须
        说出来——之前它只记日志，于是"投递成功、记录没盖上"这一状态对用户完全
        不可见，而未汇报行会让同一批内容被反复重发。
        """
        failed = [row for row in rows if not self._mark_with_retry(row.child_run_id)]
        if not failed:
            return
        self._notify_safely(conversation_id, _mark_failure_text(failed, text))

    def _mark_with_retry(self, child_run_id: str) -> bool:
        """盖章，失败重试一次；第二次仍失败时记 `logger.exception` 并返回 False。"""
        for attempt in (1, 2):
            try:
                self._store.mark_child_reported(child_run_id)
                return True
            except Exception:  # noqa: BLE001 - 盖章是尽力而为，但失败不能静默
                if attempt == 2:
                    logger.exception(
                        "标记子运行已汇报失败（已重试一次），降级为显式通知",
                        extra={"child_run_id": child_run_id},
                    )
        return False


def _invalid(message: str) -> AgentPlatformError:
    return AgentPlatformError("invalid_delegation_value", message)


__all__ = [
    "DEFAULT_MAX_INFLIGHT_CONVERSATIONS",
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "DEFAULT_THROTTLE_SECONDS",
    "WAKEUP_KEY_PREFIX",
    "ChildWakeupService",
    "render_wakeup_message",
]
