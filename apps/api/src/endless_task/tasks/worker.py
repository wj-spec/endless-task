"""Task execution worker (R4.3).

A run executes the task's commitment as an automatic turn in the task's (or
reminder's) **own execution conversation**, reusing the regular execution path.
The landing conversation is looked up by ``(internal_owner_kind,
internal_owner_id)`` and created on first use, so one task keeps exactly one
execution record and results never land in the user's daily source
conversation. If no execution conversation can be prepared (anything raised
while looking it up or creating it), the run is aborted and finished as a
**visible FAILED** run with an explicit reason, and **nothing is submitted to
the source conversation**. Degrading back to the source conversation is
forbidden (ADR-1 §S1) — that would silently write the automatic execution into
the user's daily conversation, which is exactly the defect this module fixes.
The only exception is a missing *source* conversation, which keeps its own
existing FAILED wording.

The worker drives turns on the v2 runtime: it creates a message submission on
the shared ``SqliteRuntimeV2Repository`` (binding the run to a fresh v2 run)
and executes that run through an ``AgentRunExecutor``.
"""

from __future__ import annotations

from endless_task.domain.models import TaskRecord

import asyncio
import logging
import uuid
from typing import NamedTuple, Optional

from endless_task.domain.models import (
    Conversation,
    Reminder,
    ReminderStatus,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
    TaskStatus,
)
from endless_task.domain.repositories import ConflictError
from endless_task.domain.repositories import InvalidStateError
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    RunExecutionResult,
    RunStatus,
)
from endless_task.storage import (
    SqliteChatRepository,
    SqliteReminderRepository,
    SqliteRuntimeV2Repository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)

from .notification_service import TaskNotificationService
from .run_review_service import RunReview, TaskRunReviewService

logger = logging.getLogger(__name__)

#: `conversations.internal_owner_kind` 的两个取值（迁移 074）。取值收口在仓储层
#: 的入参上、迁移里**刻意不加 CHECK**（加 CHECK 要重建表，见该迁移的注释）。
TASK_EXECUTION_OWNER_KIND = "task_execution"
REMINDER_EXECUTION_OWNER_KIND = "reminder_execution"

REMINDER_PREFIX = "【提醒】你设置的提醒到时了，请按承诺执行："

TRIGGER_PREFIX = {
    TaskRunTrigger.MANUAL: "【手动执行】请现在执行这项已确认的安排：",
    TaskRunTrigger.SCHEDULED: "【到点执行】安排已到期，请按承诺执行：",
}

# 兜底墙钟上限：正常超时由执行器按运行类别的时长预算（run_budget：soft 只提请决策、
# hard 优雅收尾）处理，这里只是防止任务执行永不返回的极端情况（对应 D11 的紧急 watchdog）。
DEFAULT_TURN_TIMEOUT_SECONDS = 24 * 60 * 60

#: 提交这一轮时目标会话已有活跃运行（`ConflictError`）时的**如实**文案。
#: 切片 10 T3 洞 2：以前这里落的是泛化的 "Turn submission failed."，用户既看不到
#: 真实原因（会话忙），也不知道会不会重跑。任务侧照既有重试语义重试（`retryable`），
#: 提醒侧不自动重跑，见 `REMINDER_CONFLICT_ERROR`。
TURN_CONFLICT_ERROR = "该会话已有正在进行的运行，这次执行没有开始。"
#: 提醒撞上活跃运行的如实文案：未执行 + 原因 + 会不会重跑（提醒 `mark_fired` 后不再被
#: 调度器选中，所以**不会**自动重跑；今天也没有手动重跑入口，不许假装有）。
REMINDER_CONFLICT_ERROR = (
    "该提醒未执行：发起提醒的会话已有正在进行的运行。"
    "提醒到时即标记为已触发，不会自动重跑。"
)
#: 切片 10 终审修复 W-G：登记取消句柄之后、真正 spawn 之前的记账步骤（建执行分支、
#: 关联落点会话、`mark_fired`）抛错时的**如实**文案——这一 run 一次都没跑起来。
TASK_BOOKKEEPING_ERROR = "这次执行没有开始：准备执行落点时出错。"
REMINDER_BOOKKEEPING_ERROR = "这次提醒没有开始：准备执行落点时出错。"

#: 批次 B Task 1（ADR-1 §S1 裁定）：**准备不出执行会话**时这一 run 的如实文案。
#:
#: 为什么必须是失败而不是"降级到源会话"：降级会把 `【到点执行】…` 静默写回用户的
#: 日常会话——正是本批要消灭的污染，而且事后只有一条日志、不可分辨。所以除"源会话
#: 已删"（那是另一个分支、语义不同）外，任何准备失败都在这里**中止**，绝不再提交。
TASK_EXECUTION_CONVERSATION_ERROR = (
    "未能准备执行会话（{reason}），已中止本次执行以免写进你的会话；可稍后重试。"
)
REMINDER_EXECUTION_CONVERSATION_ERROR = (
    "未能准备执行会话（{reason}），本次提醒没有执行，也没有写进你的会话。"
)


class ExecutionConversationUnavailable(RuntimeError):
    """准备不出该任务/提醒自己的执行会话（ADR-1 §S1）。

    调用方**不许**把它降级成"落回源会话"：那会把自动执行静默塞进用户的日常会话。
    任务侧收尾为 `FAILED`/`retryable=True`；提醒侧同样收尾为可见的 `FAILED`。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TaskTurnOutcome(NamedTuple):
    """Result of driving one task/reminder turn on the v2 runtime."""

    status: TaskRunStatus
    content: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    source_gone: bool = False
    #: 这一轮根本没提交成功，因为目标会话已有活跃运行（可重试的瞬时冲突）。
    conflicted: bool = False


def _finalize_bookkeeping_failure(
    worker: "TaskWorker",
    run_id: str,
    *,
    error: str,
    task: Optional[TaskRecord] = None,
    reminder: Optional[Reminder] = None,
) -> None:
    """登记句柄之后、真正 spawn 之前的记账步骤抛错时的收尾（切片 10 终审修复 W-G）。

    这条路径上**没有**任何 coroutine 会替这一 run 落终态：`execute` 根本没被
    spawn，而 `task_runs` 已经停在 `running`、`_cancel_tokens` 里已经登记了句柄。
    不在这里收尾，用户就会看到一个永远"运行中"的 run（占着同一 task 的单飞名额），
    而取消端点对着一个永远不会开始的 run 说"已受理"。

    收尾走既有的 `_finish(FAILED)` 路径（它顺手 pop 掉取消句柄），随后由调用方把
    原异常照原样重抛——不新增语义、不吞错误。收尾自身失败（库坏了）只记日志，
    绝不掩盖原始异常。

    `retryable` **按 subject 定**（切片 10 收尾）：这个字段会下发给客户端，而
    提醒**没有重跑入口**（R6，`mark_fired` 之后调度器再也不会选中它），落 `True`
    就是自相矛盾——与 W-C 对冲突路径的同一口径。任务侧照既有重试语义保持 `True`
    （`TaskScheduler._retry_attempt`）。
    """
    try:
        worker._finish(
            run_id,
            TaskRunStatus.FAILED,
            error=error,
            retryable=reminder is None,
            task=task,
            reminder=reminder,
        )
    except Exception:  # noqa: BLE001 - 原始异常优先，收尾失败只记日志
        logger.exception(
            "Failed to finalize task run after bookkeeping error",
            extra={"run_id": run_id},
        )


class TaskWorker:
    """Executes tasks as automatic turns in their own execution conversation.

    ``start`` / ``start_reminder`` 必须能准备出执行会话：``chat_repository``
    缺失**不算**"没有落点就退回源会话"，而是按 ADR-1 §S1 收尾为可见 FAILED
    （见两条路径里的 `ExecutionConversationUnavailable` 处理）。生产只有
    `api/wiring.py` 一处构造且会传入它，所以这条分支实际不可达，但**不留静默旁路**。
    """

    def __init__(
        self,
        *,
        task_repository: SqliteTaskRepository,
        run_repository: SqliteTaskRunRepository,
        runtime_v2_repository: SqliteRuntimeV2Repository,
        task_executor: AgentRunExecutor,
        chat_repository: Optional[SqliteChatRepository] = None,
        max_concurrent_task_runs: int = 1,
        review_service: Optional[TaskRunReviewService] = None,
        notification_service: Optional[TaskNotificationService] = None,
        reminder_repository: Optional[SqliteReminderRepository] = None,
        turn_timeout_seconds: Optional[float] = None,
    ) -> None:
        if max_concurrent_task_runs <= 0:
            raise ValueError("max_concurrent_task_runs must be positive")
        self._task_repository = task_repository
        self._run_repository = run_repository
        self._runtime_v2_repository = runtime_v2_repository
        self._task_executor = task_executor
        self._chat_repository = chat_repository
        self._semaphore = asyncio.Semaphore(max_concurrent_task_runs)
        self._task_supervisor = BackgroundTaskSupervisor(
            name="task-worker",
            logger=logger,
        )
        self._review_service = review_service
        self._notification_service = notification_service
        self._reminder_repository = reminder_repository
        self._turn_timeout_seconds = turn_timeout_seconds
        # 在飞定时/提醒 run 的可寻址取消通道（键 = `task_runs.id`，也就是 HTTP 面
        # `/tasks/{id}/runs/{run_id}` 里的那个 id）。切片 10 T3 Step 1：以前执行器
        # 拿的是**匿名** `CancellationToken()`，外部没有任何句柄，`/tasks/{id}/cancel`
        # 只改任务状态、管不到在飞的运行。
        self._cancel_tokens: dict[str, CancellationToken] = {}

    async def start(
        self,
        task_id: str,
        *,
        trigger: TaskRunTrigger,
        attempt: int = 1,
    ) -> TaskRun:
        task = self._task_repository.get_task(task_id)
        if task.status is not TaskStatus.ACTIVE:
            raise InvalidStateError("Only active tasks can be executed.")
        run = self._run_repository.claim_run(
            task_id=task_id,
            trigger=trigger,
            conversation_id=task.source_conversation_id,
            attempt=attempt,
        )
        # 占位成功即登记取消句柄：从这一刻到 `_finish` 之间，这个 run 都**可被寻址**。
        self._register_cancellation(run.id)
        # ADR-1 §S1 是全称命题："**任何**准备失败都不许降级回源会话"。所以这里没有
        # `if self._chat_repository is not None:` 的旁路：仓储缺失同样走失败收尾。
        # 留着那条旁路的话，run 的 conversation_id 会停在源会话初值、**无日志**地
        # 把 `【到点执行】…` 写进用户的日常会话。（生产只有 `api/wiring.py` 一处
        # 构造且传了它，所以这条分支实际不可达。）
        if self._chat_repository is None:
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error=TASK_EXECUTION_CONVERSATION_ERROR.format(
                    reason="未配置会话仓储"
                ),
                task=task,
                retryable=True,
            )
        try:
            branch = self._ensure_branch(
                owner_kind=TASK_EXECUTION_OWNER_KIND,
                owner_id=task.id,
                title=task.title,
                source_conversation_id=task.source_conversation_id,
            )
            run = self._run_repository.link_conversation(run.id, branch.id)
        except NotFoundError:
            logger.warning(
                "Task run %s failed: source conversation is gone", run.id
            )
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error="来源会话已删除，无法执行该安排。",
                task=task,
                retryable=False,
            )
        except ExecutionConversationUnavailable as error:
            # ADR-1 §S1：**不降级回源会话**。这一 run 收尾为可见的 FAILED，
            # 源会话里不留任何 entry；`retryable=True` 是因为下次到点（或调度器
            # 重试）会重新尝试准备执行会话，失败原因通常是瞬时的。
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error=TASK_EXECUTION_CONVERSATION_ERROR.format(reason=error),
                task=task,
                retryable=True,
            )
        except Exception:  # noqa: BLE001 - 见 W-G：不许漏句柄 / 停 running
            # 切片 10 终审修复 W-G：句柄已登记、run 已 running，这里抛错若不收尾
            # 就会留下"永远不会开始的 running run" + 泄漏一个取消句柄。
            _finalize_bookkeeping_failure(
                self,
                run.id,
                error=TASK_BOOKKEEPING_ERROR,
                task=task,
            )
            raise
        self._task_supervisor.spawn(
            self.execute(run.id),
            name=f"task-run:{run.id}",
        )
        return run

    async def execute(self, run_id: str) -> TaskRun:
        try:
            async with self._semaphore:
                return await self._execute(run_id)
        finally:
            # 兜底：任何没走到 `_finish` 的异常也不许把取消句柄留在表里。
            self._cancel_tokens.pop(run_id, None)

    async def _execute(self, run_id: str) -> TaskRun:
        run = self._run_repository.get_run(run_id)
        try:
            task = self._task_repository.get_task(run.task_id)
        except Exception:  # noqa: BLE001 - task vanished between start and run
            return self._finish(
                run_id, TaskRunStatus.CANCELLED, error="Task no longer exists."
            )
        if task.status is not TaskStatus.ACTIVE:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Task is not active anymore.",
                task=task,
            )

        message = TRIGGER_PREFIX[run.trigger] + task.commitment
        # `run.conversation_id` 现在是该任务自己的执行会话（`_ensure_branch` 之后）。
        # 这里取不到 ⇒ 落点会话在执行途中被删；文案不再点名"来源会话"（可能是源会话
        # 也可能是执行会话，说不清就说不清，见 §S2 的同一裁定）。
        outcome = await self._execute_task_turn(
            run_id=run_id,
            conversation_id=run.conversation_id,
            message=message,
            target_gone_message="执行所需的会话已删除，无法执行该安排。",
        )
        if outcome.status is TaskRunStatus.COMPLETED:
            return await self._finish_completed(
                run_id, task=task, message=message, content=outcome.content
            )
        if outcome.status is TaskRunStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                task=task,
            )
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error=outcome.error or "Turn did not complete successfully.",
            task=task,
            retryable=outcome.retryable,
        )

    async def start_reminder(self, reminder_id: str) -> TaskRun:
        if self._reminder_repository is None:
            raise InvalidStateError("Reminders are not configured.")
        reminder = self._reminder_repository.get_reminder(reminder_id)
        if reminder.status is not ReminderStatus.PENDING:
            raise InvalidStateError("Only pending reminders can be executed.")
        run = self._run_repository.claim_run(
            task_id=reminder.id,
            trigger=TaskRunTrigger.SCHEDULED,
            conversation_id=reminder.source_conversation_id,
        )
        self._register_cancellation(run.id)
        # 与 `start` 同形：没有 `is None` 旁路（ADR-1 §S1 是全称命题）。
        if self._chat_repository is None:
            self._reminder_repository.mark_fired(reminder.id)
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error=REMINDER_EXECUTION_CONVERSATION_ERROR.format(
                    reason="未配置会话仓储"
                ),
                reminder=reminder,
                retryable=False,
            )
        try:
            branch = self._ensure_branch(
                owner_kind=REMINDER_EXECUTION_OWNER_KIND,
                owner_id=reminder.id,
                title=reminder.title,
                source_conversation_id=reminder.source_conversation_id,
            )
            run = self._run_repository.link_conversation(run.id, branch.id)
        except NotFoundError:
            logger.warning(
                "Reminder run %s failed: source conversation is gone", run.id
            )
            self._reminder_repository.mark_fired(reminder.id)
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error="来源会话已删除，无法执行该提醒。",
                reminder=reminder,
            )
        except ExecutionConversationUnavailable as error:
            # ADR-1 §S1（提醒侧同样不许静默）：不落回源会话、不许"mark_fired 之后
            # 就当没发生"。收尾为**可见的 FAILED**；`mark_fired` 与既有失败分支
            # 同口径（提醒到时即 fired，`retryable=False`——提醒没有重跑入口，
            # R6），用户看到的是"没执行 + 原因"。
            logger.warning(
                "Reminder run %s aborted: no execution conversation (%s)",
                run.id,
                error,
            )
            self._reminder_repository.mark_fired(reminder.id)
            return self._finish(
                run.id,
                TaskRunStatus.FAILED,
                error=REMINDER_EXECUTION_CONVERSATION_ERROR.format(reason=error),
                reminder=reminder,
                retryable=False,
            )
        except Exception:  # noqa: BLE001 - 见 W-G：不许漏句柄 / 停 running
            # 切片 10 终审修复 W-G：与 `start` 同形的窗口（句柄已登记、run 已
            # running，spawn 还没发生），必须收尾再重抛。
            _finalize_bookkeeping_failure(
                self,
                run.id,
                error=REMINDER_BOOKKEEPING_ERROR,
                reminder=reminder,
            )
            raise
        try:
            self._reminder_repository.mark_fired(reminder.id)
        except Exception:  # noqa: BLE001 - 见 W-G：不许漏句柄 / 停 running
            _finalize_bookkeeping_failure(
                self,
                run.id,
                error=REMINDER_BOOKKEEPING_ERROR,
                reminder=reminder,
            )
            raise
        self._task_supervisor.spawn(
            self._execute_reminder(run.id, reminder),
            name=f"reminder-run:{run.id}",
        )
        return run

    async def _execute_reminder(
        self, run_id: str, reminder: Reminder
    ) -> TaskRun:
        message = REMINDER_PREFIX + reminder.commitment
        # §S2：目标会话 = **这次 run 的落点**（该提醒自己的执行会话，`start_reminder`
        # 已 `link_conversation` 过去）。取不到的是**执行会话**，所以文案点名执行会话，
        # 不再写"来源会话已删除"——只有 `_ensure_branch` 那一步（取源会话拿环境）才
        # 是"来源会话已删除"，两处文案必须能区分。
        target_gone_message = "提醒的执行会话已删除，无法执行该提醒。"
        # 此前这里硬编码回 `reminder.source_conversation_id`，让提醒路的"执行会话"
        # 即使建出来也永远是空壳（ADR-1 §1.6）。
        run = self._run_repository.get_run(run_id)
        outcome = await self._execute_task_turn(
            run_id=run_id,
            conversation_id=run.conversation_id,
            message=message,
            target_gone_message=target_gone_message,
        )
        if outcome.source_gone:
            # A disappeared source conversation still fires the reminder.
            self._reminder_repository.mark_fired(reminder.id)
        if outcome.conflicted:
            # 提醒侧不会自动重跑（`mark_fired` 之后调度器再也不会选中它），所以这里
            # 必须把"未执行 + 原因 + 会不会重跑"一次说清，而不是落一句泛化失败。
            # 切片 10 终审修复 W-C：`retryable` 必须跟这句话口径一致——提醒没有重跑
            # 入口（R6），落 True 就是自相矛盾（前端"别渲染"只是口头约束，跨批次会丢）。
            # 任务侧不动（`TaskScheduler._retry_attempt` 的既有重试语义照旧）。
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error=REMINDER_CONFLICT_ERROR,
                retryable=False,
                reminder=reminder,
            )
        if outcome.status is TaskRunStatus.COMPLETED:
            return await self._finish_completed(
                run_id,
                reminder=reminder,
                message=message,
                content=outcome.content,
            )
        if outcome.status is TaskRunStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                reminder=reminder,
            )
        # 切片 10 收尾：泛型失败分支与冲突分支同口径——`outcome.retryable` 是
        # submit/exec 失败给的（恒 True），但提醒**没有重跑入口**（R6），落 True
        # 就是同一种自相矛盾。任务侧不动（`_execute` 仍透传，既有重试语义照旧）。
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error=outcome.error or "Turn did not complete successfully.",
            retryable=False,
            reminder=reminder,
        )

    async def _execute_task_turn(
        self,
        *,
        run_id: str,
        conversation_id: str,
        message: str,
        target_gone_message: str,
    ) -> TaskTurnOutcome:
        """Submit one automatic turn on the v2 runtime and run it.

        ``conversation_id`` 是**这次 run 的落点**（该任务/提醒自己的执行会话）。
        提交时找不到它 ⇒ 落点会话在执行途中被删 ⇒ `source_gone=True`，文案由
        ``target_gone_message`` 如实给出（§S2：不再一律说"来源会话"）。

        Returns a :class:`TaskTurnOutcome`; never raises for expected failure
        modes so the journal always records the run.
        """
        try:
            submission = self._runtime_v2_repository.create_message_submission(
                conversation_id=conversation_id,
                lane_id=None,
                content=message,
                client_request_id=f"taskrun:{run_id}",
            )
        except NotFoundError:
            logger.warning(
                "Task run %s failed: the landing conversation is gone", run_id
            )
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error=target_gone_message,
                retryable=False,
                source_gone=True,
            )
        except ConflictError as error:
            # 目标会话已有活跃运行（`storage/v2/transcript.py` 的提交闸）。
            # 按异常类型分派：**不**落泛化文案，如实说清"没执行 + 为什么"。
            logger.warning(
                "Task run %s could not submit a turn: conversation is busy (%s)",
                run_id,
                error,
            )
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error=TURN_CONFLICT_ERROR,
                retryable=True,
                conflicted=True,
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed to submit turn: %s", run_id, error)
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error="Turn submission failed.",
                retryable=True,
            )

        # Map the task run to the v2 run id (surfaced as `turnId`).
        self._run_repository.link_turn(run_id, submission.run.id)

        if not submission.created:
            # Idempotent re-submission: the v2 run already exists and was
            # (or is being) executed; only derive its status, never re-run it.
            existing = self._runtime_v2_repository.get_run(submission.run.id)
            return self._map_status(existing.status, None)

        try:
            result = await self._execute_with_guard(
                submission.run.id,
                cancellation_token=self._cancellation_token(run_id),
            )
        except RuntimeCancelled:
            return TaskTurnOutcome(
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed while waiting: %s", run_id, error)
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error="Turn execution failed.",
                retryable=True,
            )
        return self._map_status(result.status, result.content or None)

    async def _execute_with_guard(
        self, run_id: str, *, cancellation_token: CancellationToken
    ) -> RunExecutionResult:
        """Execute a v2 run under a wall-clock guard so tasks never hang.

        `cancellation_token` 由 worker 按 `task_runs.id` 持有（见 `cancel`），
        所以外部可以真的把这一轮取消到终态，而不是只能改任务状态。
        """
        timeout = self._turn_timeout_seconds
        if timeout is None:
            timeout = DEFAULT_TURN_TIMEOUT_SECONDS
        return await asyncio.wait_for(
            self._task_executor.execute(
                run_id,
                cancellation_token=cancellation_token,
            ),
            timeout=timeout,
        )

    def _register_cancellation(self, run_id: str) -> CancellationToken:
        token = CancellationToken()
        self._cancel_tokens[run_id] = token
        return token

    def _cancellation_token(self, run_id: str) -> CancellationToken:
        """取这个 run 的取消句柄；兜底自建（直接调 `execute` 的调用方也能取消）。"""
        token = self._cancel_tokens.get(run_id)
        if token is None:
            token = self._register_cancellation(run_id)
        return token

    def cancel(self, run_id: str) -> bool:
        """请求优雅取消一个在飞的定时/提醒 run；没有在飞句柄时**如实**返回 False。

        走的是执行器既有的取消语义（`CancellationToken` → `RuntimeCancelled` →
        终态收尾），不新增任何强制杀路径。终态由 `_execute`/`_execute_reminder`
        经 `_finish` 落库，并顺手释放并发信号量（下一个到期任务随即被放行）。
        """
        token = self._cancel_tokens.get(run_id)
        if token is None or token.is_cancelled:
            return False
        token.cancel()
        return True

    def in_flight(self, run_id: str) -> bool:
        """这个进程里是否持有该 run 的**未取消**在飞句柄。"""
        token = self._cancel_tokens.get(run_id)
        return token is not None and not token.is_cancelled

    @staticmethod
    def _map_status(status: RunStatus, content: Optional[str]) -> TaskTurnOutcome:
        if status is RunStatus.COMPLETED:
            return TaskTurnOutcome(TaskRunStatus.COMPLETED, content=content)
        if status is RunStatus.CANCELLED:
            return TaskTurnOutcome(
                TaskRunStatus.CANCELLED, error="Turn was cancelled."
            )
        return TaskTurnOutcome(
            TaskRunStatus.FAILED,
            error="Turn did not complete successfully.",
            retryable=True,
        )

    def _ensure_branch(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        title: str,
        source_conversation_id: str,
    ) -> Conversation:
        """按 owner 查/建这条任务（或提醒）**自己的执行会话**（ADR-1 C 形状）。

        一个任务一份执行记录：先按 `(owner_kind, owner_id)` 找回既有执行会话并复用；
        找不到才新建一条 `kind='normal'`、`parent_conversation_id=NULL` 的正式会话，
        环境（workspace / provider profile / model override）继承源会话——本批只改
        **落点会话**，不改执行环境。

        **失败绝不降级回源会话**（ADR-1 §S1）：降级会把 `【到点执行】…` 静默写回
        用户的日常会话（正是本批要消灭的污染），事后只有一条日志、不可分辨。所以：

        - 源会话已删 → 照旧抛 `NotFoundError`，调用方走既有
          `_finish(FAILED, "来源会话已删除…")` 语义（文案与行为逐字不变）；
        - 其余任何异常（建会话失败、库出错…）→ 抛
          :class:`ExecutionConversationUnavailable`，调用方把这一 run 收尾为**可见的
          FAILED**，源会话里不留任何 entry。
        """
        assert self._chat_repository is not None
        existing = self._chat_repository.find_internal_conversation(owner_kind, owner_id)
        if existing is not None:
            return existing
        source = self._chat_repository.get_conversation(source_conversation_id)
        try:
            return self._chat_repository.create_internal_conversation(
                owner_kind=owner_kind,
                owner_id=owner_id,
                title=self._execution_title(title),
                workspace_id=source.workspace_id,
                provider_profile_id=source.provider_profile_id,
                model_override=source.model_override,
            )
        except Exception as error:  # noqa: BLE001 - 见 docstring：不许静默落回源会话
            logger.warning(
                "Run owner %s:%s could not get an execution conversation (%s); "
                "aborting the run instead of writing into the source conversation %s",
                owner_kind,
                owner_id,
                error,
                source_conversation_id,
            )
            raise ExecutionConversationUnavailable(str(error)) from error
    @staticmethod
    def _execution_title(title: str) -> str:
        """执行会话标题：`《<任务标题>》· 执行记录`（ADR-1 §3.4）。

        旧形态 `《…》· 执行 @MM-DD HH:MM` 是"每次到点一条会话"的产物；改成一份
        执行记录后时间戳不再区分任何东西，反而会让复用判据看着像失效。
        """
        return f"《{title}》· 执行记录"

    def _finish(
        self,
        run_id: str,
        status: TaskRunStatus,
        *,
        error: Optional[str] = None,
        awaiting_user: bool = False,
        awaiting_note: Optional[str] = None,
        retryable: bool = False,
        task: Optional[TaskRecord] = None,
        reminder: Optional[Reminder] = None,
        excerpt: Optional[str] = None,
    ) -> TaskRun:
        try:
            run = self._run_repository.finish_run(
                run_id,
                status,
                error=error,
                awaiting_user=awaiting_user,
                awaiting_note=awaiting_note,
                retryable=retryable,
            )
        finally:
            # 终态即释放取消句柄：此后 `cancel` 如实返回 False（同一 run 只会终态一次）。
            self._cancel_tokens.pop(run_id, None)
        if self._notification_service is not None:
            try:
                if task is not None:
                    self._notification_service.notify_run(task, run, excerpt)
                elif reminder is not None:
                    self._notification_service.notify_reminder(
                        reminder, run, excerpt
                    )
            except Exception:  # noqa: BLE001 - notifications must not fail runs
                logger.warning(
                    "Task run notification skipped for %s",
                    run_id,
                    exc_info=True,
                )
        return run

    @staticmethod
    def _excerpt(content: Optional[str]) -> Optional[str]:
        if not content or not content.strip():
            return None
        return content

    async def _finish_completed(
        self,
        run_id: str,
        *,
        message: str,
        content: Optional[str],
        task: Optional[TaskRecord] = None,
        reminder: Optional[Reminder] = None,
    ) -> TaskRun:
        review = await self._review_run(message, content)
        return self._finish(
            run_id,
            TaskRunStatus.COMPLETED,
            awaiting_user=review.awaiting_user,
            awaiting_note=review.note,
            task=task,
            reminder=reminder,
            excerpt=self._excerpt(content),
        )

    async def _review_run(
        self, message: str, content: Optional[str]
    ) -> RunReview:
        if self._review_service is None:
            return RunReview(False)
        if content is None:
            return RunReview(False)
        return await self._review_service.review(
            user_message=message,
            assistant_message=content,
        )

    async def drain(self) -> None:
        await self._task_supervisor.drain()

    def running_count(self) -> int:
        return self._task_supervisor.running_count()

    @property
    def _tasks(self) -> tuple[asyncio.Task[object], ...]:
        return self._task_supervisor.tasks
