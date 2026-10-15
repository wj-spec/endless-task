import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { EmptyState } from "../ui/EmptyState";
import { ApiClientError, chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import { subscribeHubEvents } from "../hub/hubEvents";
import type { Reminder, TaskRun, TaskSummary } from "../chat/apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { RowMenu } from "../ui/RowMenu";
import type { RowMenuItem } from "../ui/RowMenu";
import type { TaskSource } from "./taskSource";
import {
  TASK_SCHEDULE_KINDS,
  TASK_SCHEDULE_KIND_LABEL,
  TASK_WEEKDAY_OPTIONS,
  buildCreateTaskPayload,
  buildUpdateTaskPayload,
  createTaskErrorText,
  emptyTaskFormValues,
  taskFormValuesFromTask,
  updateTaskErrorText,
  validateTaskForm,
} from "./taskForm";
import type {
  TaskFormField,
  TaskFormFieldErrors,
  TaskFormValues,
  TaskScheduleKind,
} from "./taskForm";
import {
  buildCreateReminderPayload,
  createReminderErrorText,
  emptyReminderFormValues,
  validateReminderForm,
} from "./reminderForm";
import type {
  ReminderFormField,
  ReminderFormFieldErrors,
  ReminderFormValues,
} from "./reminderForm";

type ScheduledTasksContentProps = {
  onOpenConversation: (conversationId: string) => void;
  /**
   * 切片 10 T6：新建定时任务要绑定的**来源锚点**（当前会话 + 当前轮次）。
   *
   * 由 `AssistantPanel` 用 `resolveTaskSource(conversationId, runtimeSnapshot)` 算好传下来
   * （见 `taskSource.ts` 的推导与证据）。缺省 `null` = 拿不到当前轮次 → 表单**禁止提交**：
   * 后端 `POST /tasks` 的 `conversationId`/`turnId` 必填，这里绝不编 id、也不假成功。
   */
  taskSource?: TaskSource | null;
};

const STATUS_LABEL: Record<TaskSummary["status"], string> = {
  active: "进行中",
  paused: "已暂停",
  cancelled: "已取消",
};

const RUN_STATUS_LABEL: Record<TaskRun["status"], string> = {
  running: "执行中",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const RUN_TRIGGER_LABEL: Record<TaskRun["trigger"], string> = {
  manual: "手动执行",
  scheduled: "到点执行",
};

const REMINDER_STATUS_LABEL: Record<Reminder["status"], string> = {
  pending: "待执行",
  fired: "已执行",
  cancelled: "已取消",
};

const POLL_DELAYS_MS = [2000, 6000, 12000];

/**
 * 切片 10 T4（item 3）：会让「已安排」面板失效的 hub 事件类型。
 *
 * 后端**只有任务/提醒的终态**才经 `notification.created` 广播（T3 §8：run 的
 * 创建/开始不发 hub 事件，CANCELLED 也不发），所以只订阅这一种；缺口由"面板重新
 * 可见时补拉"兜底，见下面的 focus/visibility 订阅——不引入常驻轮询定时器。
 */
const HUB_REFRESH_EVENT_TYPES = new Set(["notification.created"]);

/**
 * 切片 10 A2（item 8）：状态类操作（暂停/恢复/取消）失败的**如实归因**。
 *
 * `changeStatus` / `cancelReminder` 此前把**所有**失败塌成一句「操作失败，请重试。」——
 * 与 T6 修掉的创建归因错位**同形**。后端就两种真实原因（`api/app.py:_repository_error_status`）：
 * 404 `NotFoundError`（这条安排/提醒已经不存在了）、409 `ConflictError`/`InvalidStateError`
 * （状态已经变了：不是 active 不能暂停/恢复，已取消不能再取消）。两者用户能做的下一步
 * 完全不同，网络不通也不是"重试"能保证的——所以逐类指名；认不出来的原因**原样**带上
 * 后端 message，既不吞、也不装成同一句话。
 *
 * 切片 10 A3（Fix 2）：`runNow`（"立即执行一次"）此前仍把**所有**失败塌成一句
 * 「触发执行失败，请重试。」——同形的归因错位。它复用这条映射（同一个执行端点
 * `POST /tasks/{id}/run`，与 `retryRun` 一致），只是 409 的说明另按该端点的真实原因指名。
 */
const TASK_ACTION_LABEL = {
  pause: "暂停",
  resume: "恢复",
  cancel: "取消安排",
  cancelReminder: "取消提醒",
  runNow: "触发执行",
} as const;

export type TaskAction = keyof typeof TASK_ACTION_LABEL;

/**
 * 切片 10 A3（Fix 2）：409 冲突的**逐类**说明。
 *
 * 缺省那句覆盖状态类操作（暂停/恢复/取消）的真实原因；`runNow` 走执行端点，
 * 409 的两个真实原因是"已有在飞的运行"（`This task is already running.`）/
 * "任务不在进行中"（`Only active tasks can be executed.`）——套用状态类那句会把
 * "已经在跑"误说成"状态变了、已不在进行中"，所以单独指名。
 */
const TASK_ACTION_CONFLICT_REASON: Partial<Record<TaskAction, string>> = {
  runNow: "这个安排已有正在执行的运行，或者它已经不在进行中",
};

export const taskActionErrorText = (
  action: TaskAction,
  error: unknown,
): string => {
  const verb = TASK_ACTION_LABEL[action];
  const subject = action === "cancelReminder" ? "提醒" : "安排";
  if (!(error instanceof ApiClientError)) {
    return `${verb}没有提交成功：连不上本地服务，请确认 API 已启动。`;
  }
  if (error.status === 404) {
    return `这条${subject}已经不存在了（可能已在别处删除），${verb}没有生效。`;
  }
  if (error.status === 409) {
    const reason =
      TASK_ACTION_CONFLICT_REASON[action] ??
      `这条${subject}的状态已经变了（可能刚被取消，或者已不在进行中）`;
    return `现在不能${verb}：${reason}。`;
  }
  return `${verb}没有成功（HTTP ${error.status}）：${error.message}`;
};

/**
 * 切片 10 T4（item 4）：重试失败的**如实**文案。
 *
 * 重试端点就是既有的 `POST /tasks/{id}/run`（202）。409 有两个真实原因：已有在飞
 * 运行（`This task is already running.`）/ 任务不在进行中（`Only active tasks can be
 * executed.`）。后端文案是英文，这里给等价的中文说明；其它失败原因一律回落到可读
 * 错误文本，不吞、也不谎报成功。
 */
const retryFailureText = (error: unknown): string => {
  if (error instanceof ApiClientError && error.status === 409) {
    return "现在不能重跑：这个安排已有正在执行的运行，或者它已经不在进行中。";
  }
  const detail = readableError(error);
  return detail ? `重试没有提交成功：${detail}` : "重试没有提交成功，请稍后再试。";
};

type PendingCancel = {
  kind: "task" | "reminder";
  id: string;
  title: string;
};

function formatDue(dueAt: string): string {
  const moment = new Date(dueAt);
  if (Number.isNaN(moment.getTime())) return dueAt;
  const hh = String(moment.getHours()).padStart(2, "0");
  const mm = String(moment.getMinutes()).padStart(2, "0");
  return `${moment.getMonth() + 1}月${moment.getDate()}日 ${hh}:${mm}`;
}

export function ScheduledTasksContent({
  onOpenConversation,
  taskSource = null,
}: ScheduledTasksContentProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [runsByTask, setRunsByTask] = useState<Record<string, TaskRun[]>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingCancel, setPendingCancel] = useState<PendingCancel | null>(null);
  // 切片 10 T6：新建表单的状态。`fieldErrors` 是提交前校验（本地）的结果，
  // `createError` 是**后端**如实报错。两者分开，谁也不吞谁。
  const [createOpen, setCreateOpen] = useState(false);
  const [values, setValues] = useState<TaskFormValues>(emptyTaskFormValues);
  const [fieldErrors, setFieldErrors] = useState<TaskFormFieldErrors>({});
  const [createError, setCreateError] = useState<string | null>(null);
  const [createNotice, setCreateNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 批次 D Task 1：新建提醒表单（落「已安排」面板，**不新增页签**）。
  const [reminderOpen, setReminderOpen] = useState(false);
  const [reminderValues, setReminderValues] = useState<ReminderFormValues>(
    emptyReminderFormValues,
  );
  const [reminderFieldErrors, setReminderFieldErrors] =
    useState<ReminderFormFieldErrors>({});
  const [reminderError, setReminderError] = useState<string | null>(null);
  const [reminderNotice, setReminderNotice] = useState<string | null>(null);
  const [reminderSubmitting, setReminderSubmitting] = useState(false);
  // 批次 D Task 1：任务编辑表单（入口在任务行菜单的「编辑」）。`editingTask` 既是
  // "表单开着没开"的开关，也是"在改哪一条"的句柄；`editValues` 是它的预填副本。
  const [editingTask, setEditingTask] = useState<TaskSummary | null>(null);
  const [editValues, setEditValues] = useState<TaskFormValues | null>(null);
  const [editFieldErrors, setEditFieldErrors] = useState<TaskFormFieldErrors>({});
  const [editError, setEditError] = useState<string | null>(null);
  const [editNotice, setEditNotice] = useState<string | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const formId = useId();
  const reminderFormId = useId();
  const editFormId = useId();
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const loadRuns = useCallback(async (taskId: string) => {
    try {
      const runs = await chatApi.listTaskRuns(taskId);
      setRunsByTask((current) => ({ ...current, [taskId]: runs }));
    } catch {
      // 执行记录是增强信息，失败时静默降级。
    }
  }, []);

  const load = useCallback(async (options: { silent?: boolean } = {}) => {
    try {
      const items = await chatApi.listTasks();
      setTasks(items);
      const reminderItems = await chatApi.listReminders();
      setReminders(reminderItems);
      setLoadError(null);
      await Promise.all(
        [...items.map((task) => task.id), ...reminderItems.map((r) => r.id)].map(
          (id) => loadRuns(id),
        ),
      );
    } catch {
      // 后台刷新（hub 事件 / 重新可见）失败时保留已有列表：把整块面板换成错误态
      // 是过度反应，而且会让用户以为数据没了。首屏加载失败才显示错误态。
      if (!options.silent) setLoadError("无法加载已安排的事项，请重试。");
    } finally {
      setLoading(false);
    }
  }, [loadRuns]);

  useEffect(() => {
    void load();
    return () => {
      timers.current.forEach((timer) => globalThis.clearTimeout(timer));
    };
  }, [load]);

  /**
   * 切片 10 T6：新建定时任务。
   *
   * 顺序是**先本地校验、再提交**（验收 ③ 的口径：挡在提交前，不半做）。校验通过后
   * 组包（恰好契约里那 5 个键）再 `POST /tasks`；**只有**真的 201 才清空表单 + 刷新
   * 列表 + 报成功；任何失败都如实显示（`createTaskErrorText` 映射后端指名 400 码），
   * 并保留用户已填内容——不静默、不假装成功。
   */
  const updateField = (field: TaskFormField, value: string) => {
    setValues((current) => {
      const next = { ...current };
      next[field] = value;
      return next;
    });
    setFieldErrors((current) => {
      if (!(field in current)) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
  };

  const submitCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    const errors = validateTaskForm(values);
    setFieldErrors(errors);
    setCreateError(null);
    setCreateNotice(null);
    if (Object.keys(errors).length > 0) return;
    if (!taskSource) {
      setCreateError(
        "没有当前会话/当前轮次可以作为来源，创建没有提交（定时任务必须绑定来源会话与轮次）。",
      );
      return;
    }
    setSubmitting(true);
    try {
      const created = await chatApi.createTask(
        buildCreateTaskPayload(values, taskSource),
      );
      setValues(emptyTaskFormValues());
      setCreateNotice(`已创建《${created.title}》。`);
      await load();
    } catch (error) {
      setCreateError(createTaskErrorText(error));
    } finally {
      setSubmitting(false);
    }
  };

  /* ------------------------------------------------ 批次 D Task 1：新建提醒（自己的词表） */

  /**
   * 与 `submitCreate` 同形：先本地校验（形状/真实性）、再提交、**只有真的 201**
   * 才清空 + 刷新 + 报成功；失败如实显示并保留已填内容。
   *
   * 「时间是否已过去」**不**在本地判：那一条的权威边界在后端（按分钟、按 UTC 归一
   * 后比较），前端再抄一份只会有两种口径。后端回 `reminder_due_in_past`，这里映射
   * 成可见文案。
   */
  const updateReminderField = (field: ReminderFormField, value: string) => {
    setReminderValues((current) => ({ ...current, [field]: value }));
    setReminderFieldErrors((current) => {
      if (!(field in current)) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
  };

  const submitReminder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (reminderSubmitting) return;
    const errors = validateReminderForm(reminderValues);
    setReminderFieldErrors(errors);
    setReminderError(null);
    setReminderNotice(null);
    if (Object.keys(errors).length > 0) return;
    if (!taskSource) {
      setReminderError(
        "没有当前会话/当前轮次可以作为来源，创建没有提交（提醒必须绑定来源会话与轮次）。",
      );
      return;
    }
    setReminderSubmitting(true);
    try {
      const created = await chatApi.createReminder(
        buildCreateReminderPayload(reminderValues, taskSource),
      );
      setReminderValues(emptyReminderFormValues());
      setReminderNotice(`已创建《${created.title}》。`);
      await load();
    } catch (error) {
      setReminderError(createReminderErrorText(error));
    } finally {
      setReminderSubmitting(false);
    }
  };

  /* ------------------------------------------------ 批次 D Task 1：编辑安排（PATCH /tasks/{id}） */

  const openTaskEditor = (task: TaskSummary) => {
    setEditingTask(task);
    setEditValues(taskFormValuesFromTask(task));
    setEditFieldErrors({});
    setEditError(null);
    setEditNotice(null);
  };

  const closeTaskEditor = () => {
    setEditingTask(null);
    setEditValues(null);
    setEditFieldErrors({});
    setEditError(null);
  };

  const updateEditField = (field: TaskFormField, value: string) => {
    setEditValues((current) => (current ? { ...current, [field]: value } : current));
    setEditFieldErrors((current) => {
      if (!(field in current)) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
  };

  /** 频度是**枚举**而不是字符串字段（与新建表单同一处理），单独一个入口。 */
  const updateEditKind = (kind: TaskScheduleKind) => {
    setEditValues((current) => (current ? { ...current, kind } : current));
    setEditFieldErrors({});
    setEditError(null);
  };

  /**
   * 保存编辑：`PATCH /tasks/{id}`。
   *
   * `buildUpdateTaskPayload` 会把三件（title/commitment/schedule）**整份**送上去
   * ——排期是原样回填的。后端按解析后的**语义**判断"排期到底变没变"，原样回写不会
   * 重置触发锚点（R-D7），所以这里不需要在前端做差分（那是语义，只有后端一处权威）。
   *
   * 成功才关表单 + 报成功 + 刷新列表；失败**保留已填内容**并逐类归因。
   */
  const submitEdit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (editSubmitting) return;
    const task = editingTask;
    const values = editValues;
    if (!task || !values) return;
    const errors = validateTaskForm(values);
    setEditFieldErrors(errors);
    setEditError(null);
    setEditNotice(null);
    if (Object.keys(errors).length > 0) return;
    setEditSubmitting(true);
    try {
      const updated = await chatApi.updateTask(
        task.id,
        buildUpdateTaskPayload(values),
      );
      setEditingTask(null);
      setEditValues(null);
      setEditNotice(`已保存《${updated.title}》，列表已刷新。`);
      await load();
    } catch (error) {
      setEditError(updateTaskErrorText(error));
    } finally {
      setEditSubmitting(false);
    }
  };

  // 切片 10 T4（item 3）：面板此前只在挂载时 load 一次，后端到点触发的任务/运行
  // 在面板上永远停在旧值。这里挂在**既有**那条 hub 流上（`useAssistantHub` 广播
  // 事件类型），收到任务/提醒终态就重拉任务、提醒与各自的最近一次 run。
  useEffect(
    () =>
      subscribeHubEvents((type) => {
        if (!HUB_REFRESH_EVENT_TYPES.has(type)) return;
        void load({ silent: true });
      }),
    [load],
  );

  // 兜底（后端如实缺口，T3 §8）：run 的**创建/开始**不发 hub 事件，所以"由后端到点
  // 触发的任务刚开始跑"这一刻没有推送。复用 `useAssistantHub` 的同款范式——窗口
  // 重新获得焦点 / 重新可见时补拉一次，而不是加一个常驻轮询定时器。
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "hidden") return;
      void load({ silent: true });
    };
    globalThis.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      globalThis.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [load]);

  const changeStatus = async (
    taskId: string,
    action: "pause" | "resume" | "cancel",
  ) => {
    setBusyTaskId(taskId);
    setActionError(null);
    try {
      if (action === "pause") {
        await chatApi.pauseTask(taskId);
      } else if (action === "resume") {
        await chatApi.resumeTask(taskId);
      } else {
        await chatApi.cancelTask(taskId);
      }
      await load();
    } catch (error) {
      setActionError(taskActionErrorText(action, error));
    } finally {
      setBusyTaskId(null);
    }
  };

  const cancelReminder = async (reminderId: string) => {
    setBusyTaskId(reminderId);
    setActionError(null);
    try {
      await chatApi.cancelReminder(reminderId);
      await load();
    } catch (error) {
      setActionError(taskActionErrorText("cancelReminder", error));
    } finally {
      setBusyTaskId(null);
    }
  };

  const requestCancel = (task: TaskSummary) => {
    setPendingCancel({ kind: "task", id: task.id, title: task.title });
  };

  const confirmPendingCancel = () => {
    if (!pendingCancel) return;
    if (pendingCancel.kind === "task") {
      void changeStatus(pendingCancel.id, "cancel");
    } else {
      void cancelReminder(pendingCancel.id);
    }
  };

  /**
   * 切片 10 A3（Fix 2）："立即执行一次"的失败**逐类**指名。
   *
   * 此前这里 `catch {}` 把所有失败塌成「触发执行失败，请重试。」——网络不通、
   * 这条安排已被删、已经在跑/不在进行中，用户能做的下一步完全不同。端点就是
   * `POST /tasks/{id}/run`，与 `retryRun` 同一个，所以复用同一条逐类映射。
   */
  const runNow = async (taskId: string) => {
    setBusyTaskId(taskId);
    setActionError(null);
    try {
      await chatApi.runTask(taskId);
      POLL_DELAYS_MS.forEach((delay) => {
        timers.current.push(
          globalThis.setTimeout(() => void loadRuns(taskId), delay),
        );
      });
    } catch (error) {
      setActionError(taskActionErrorText("runNow", error));
    } finally {
      setBusyTaskId(null);
    }
  };

  /**
   * 切片 10 T4（item 4）：失败 run 的"重试"。
   *
   * 走的就是既有重试端点 `POST /tasks/{id}/run`（`chatApi.runTask`），并沿用
   * `runNow` 的 `[2s,6s,12s]` 补拉范式让新 run 尽快落到终态。**只对任务**提供：
   * 提醒没有重跑端点（后端 `worker.py:124` 让提醒 run 的 `retryable` 恒为 false，
   * 见 T3 §8），点了必然 404，所以提醒行不渲染重试按钮。
   */
  const retryRun = async (taskId: string) => {
    setBusyTaskId(taskId);
    setActionError(null);
    try {
      await chatApi.runTask(taskId);
      POLL_DELAYS_MS.forEach((delay) => {
        timers.current.push(
          globalThis.setTimeout(() => void loadRuns(taskId), delay),
        );
      });
      await loadRuns(taskId);
    } catch (error) {
      setActionError(retryFailureText(error));
    } finally {
      setBusyTaskId(null);
    }
  };

  return (
    <>
      <div className="panel-content">
          {/* 切片 10 T6：新建入口。面板此前只能"看"助手抽取来的提案，这里补上显式创建。 */}
          <div className="task-create">
            <button
              aria-controls={`${formId}-form`}
              aria-expanded={createOpen}
              className="task-create-toggle"
              onClick={() => setCreateOpen((open) => !open)}
              type="button"
            >
              {createOpen ? "收起新建表单" : "＋ 新建定时任务"}
            </button>
            {createOpen ? (
              <form
                aria-label="新建定时任务"
                className="task-create-form"
                id={`${formId}-form`}
                onSubmit={submitCreate}
              >
                <label htmlFor={`${formId}-title`}>任务标题</label>
                <input
                  aria-describedby={
                    fieldErrors.title ? `${formId}-title-error` : undefined
                  }
                  aria-invalid={fieldErrors.title ? true : undefined}
                  disabled={submitting}
                  id={`${formId}-title`}
                  onChange={(event) => updateField("title", event.target.value)}
                  placeholder="例如：每周发布检查"
                  value={values.title}
                />
                {fieldErrors.title ? (
                  <p
                    className="task-create-error"
                    id={`${formId}-title-error`}
                    role="alert"
                  >
                    {fieldErrors.title}
                  </p>
                ) : null}

                <label htmlFor={`${formId}-commitment`}>到点执行的内容</label>
                <textarea
                  aria-describedby={
                    fieldErrors.commitment
                      ? `${formId}-commitment-error`
                      : undefined
                  }
                  aria-invalid={fieldErrors.commitment ? true : undefined}
                  disabled={submitting}
                  id={`${formId}-commitment`}
                  onChange={(event) =>
                    updateField("commitment", event.target.value)
                  }
                  placeholder="到点后作为消息发给助手的内容，例如：总结上周的项目进展"
                  rows={3}
                  value={values.commitment}
                />
                {fieldErrors.commitment ? (
                  <p
                    className="task-create-error"
                    id={`${formId}-commitment-error`}
                    role="alert"
                  >
                    {fieldErrors.commitment}
                  </p>
                ) : null}

                <label htmlFor={`${formId}-kind`}>执行周期</label>
                {/* `once`（一次性事项）**刻意不提供**：后端 parse_task_schedule 直接 400
                    （"一次性事项不属于任务"）。 */}
                <select
                  disabled={submitting}
                  id={`${formId}-kind`}
                  onChange={(event) => {
                    const kind = event.target.value as TaskScheduleKind;
                    setValues((current) => ({ ...current, kind }));
                    setFieldErrors({});
                    setCreateError(null);
                    setCreateNotice(null);
                  }}
                  value={values.kind}
                >
                  {TASK_SCHEDULE_KINDS.map((kind) => (
                    <option key={kind} value={kind}>
                      {TASK_SCHEDULE_KIND_LABEL[kind]}
                    </option>
                  ))}
                </select>

                <label htmlFor={`${formId}-time`}>时间（HH:MM，24 小时制）</label>
                {/* 用文本输入而不是 `type="time"`：非法值要能被**本地校验**明确挡下并给出
                    同一口径的文案，而不是被浏览器/测试环境悄悄清空成"没填"。 */}
                <input
                  aria-describedby={
                    fieldErrors.time ? `${formId}-time-error` : undefined
                  }
                  aria-invalid={fieldErrors.time ? true : undefined}
                  disabled={submitting}
                  id={`${formId}-time`}
                  inputMode="numeric"
                  onChange={(event) => updateField("time", event.target.value)}
                  placeholder="09:00"
                  value={values.time}
                />
                {fieldErrors.time ? (
                  <p
                    className="task-create-error"
                    id={`${formId}-time-error`}
                    role="alert"
                  >
                    {fieldErrors.time}
                  </p>
                ) : null}

                {values.kind === "weekly" ? (
                  <>
                    <label htmlFor={`${formId}-weekday`}>周几</label>
                    <select
                      aria-describedby={
                        fieldErrors.weekday
                          ? `${formId}-weekday-error`
                          : undefined
                      }
                      aria-invalid={fieldErrors.weekday ? true : undefined}
                      disabled={submitting}
                      id={`${formId}-weekday`}
                      onChange={(event) =>
                        updateField("weekday", event.target.value)
                      }
                      value={values.weekday}
                    >
                      <option value="">请选择</option>
                      {TASK_WEEKDAY_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    {fieldErrors.weekday ? (
                      <p
                        className="task-create-error"
                        id={`${formId}-weekday-error`}
                        role="alert"
                      >
                        {fieldErrors.weekday}
                      </p>
                    ) : null}
                  </>
                ) : null}

                {values.kind === "monthly" ? (
                  <>
                    <label htmlFor={`${formId}-day`}>每月几号（1–28）</label>
                    {/* 刻意**不挂** `min`/`max`：浏览器原生约束校验会在 submit 事件之前
                        就掐掉提交，用户看到的是原生气泡而不是下面那条可见文案——那就是"两
                        种各做一半"。这里只留**一条**闸门：本地校验 + 可见的字段级文案。 */}
                    <input
                      aria-describedby={
                        fieldErrors.day ? `${formId}-day-error` : undefined
                      }
                      aria-invalid={fieldErrors.day ? true : undefined}
                      disabled={submitting}
                      id={`${formId}-day`}
                      inputMode="numeric"
                      onChange={(event) => updateField("day", event.target.value)}
                      placeholder="1"
                      type="number"
                      value={values.day}
                    />
                    {fieldErrors.day ? (
                      <p
                        className="task-create-error"
                        id={`${formId}-day-error`}
                        role="alert"
                      >
                        {fieldErrors.day}
                      </p>
                    ) : null}
                  </>
                ) : null}

                <p className="task-create-hint">
                  {taskSource
                    ? "来源：当前会话的当前轮次（这条安排会记住它是从哪一轮来的）。"
                    : "拿不到当前会话的当前轮次，暂时不能新建：定时任务必须绑定来源会话与轮次。"}
                </p>

                <div className="task-create-actions">
                  <button disabled={submitting || !taskSource} type="submit">
                    {submitting ? "创建中…" : "创建"}
                  </button>
                </div>

                {createError ? (
                  <p className="task-create-error" role="alert">
                    {createError}
                  </p>
                ) : null}
                {createNotice ? (
                  <p className="task-create-notice" role="status">
                    {createNotice}
                  </p>
                ) : null}
              </form>
            ) : null}
          </div>

          {/* 批次 D Task 1：「新建提醒」。落**同一个**「已安排」面板（不新增页签），
              词表是自己的（只有 `once` + `YYYY-MM-DDTHH:MM`，见 `reminderForm.ts`）。 */}
          <div className="task-create">
            <button
              aria-controls={`${reminderFormId}-form`}
              aria-expanded={reminderOpen}
              className="task-create-toggle"
              onClick={() => setReminderOpen((open) => !open)}
              type="button"
            >
              {reminderOpen ? "收起新建提醒" : "＋ 新建提醒"}
            </button>
            {reminderOpen ? (
              <form
                aria-label="新建提醒"
                className="task-create-form reminder-create-form"
                id={`${reminderFormId}-form`}
                onSubmit={submitReminder}
              >
                <label htmlFor={`${reminderFormId}-title`}>提醒标题</label>
                <input
                  aria-describedby={
                    reminderFieldErrors.title
                      ? `${reminderFormId}-title-error`
                      : undefined
                  }
                  aria-invalid={reminderFieldErrors.title ? true : undefined}
                  disabled={reminderSubmitting}
                  id={`${reminderFormId}-title`}
                  onChange={(event) =>
                    updateReminderField("title", event.target.value)
                  }
                  placeholder="例如：给妈妈打电话"
                  value={reminderValues.title}
                />
                {reminderFieldErrors.title ? (
                  <p
                    className="task-create-error"
                    id={`${reminderFormId}-title-error`}
                    role="alert"
                  >
                    {reminderFieldErrors.title}
                  </p>
                ) : null}

                <label htmlFor={`${reminderFormId}-commitment`}>
                  到点提醒我做什么
                </label>
                <textarea
                  aria-describedby={
                    reminderFieldErrors.commitment
                      ? `${reminderFormId}-commitment-error`
                      : undefined
                  }
                  aria-invalid={
                    reminderFieldErrors.commitment ? true : undefined
                  }
                  disabled={reminderSubmitting}
                  id={`${reminderFormId}-commitment`}
                  onChange={(event) =>
                    updateReminderField("commitment", event.target.value)
                  }
                  placeholder="到点后作为消息发给助手的内容，例如：打个电话问问家里情况"
                  rows={3}
                  value={reminderValues.commitment}
                />
                {reminderFieldErrors.commitment ? (
                  <p
                    className="task-create-error"
                    id={`${reminderFormId}-commitment-error`}
                    role="alert"
                  >
                    {reminderFieldErrors.commitment}
                  </p>
                ) : null}

                <label htmlFor={`${reminderFormId}-at`}>
                  提醒时间（YYYY-MM-DDTHH:MM，本地时间）
                </label>
                {/* 与任务表单同一个理由：用文本输入而不是 `type="datetime-local"`，
                    非法值要能被**本地校验**明确挡下并给出同一口径的文案，而不是被
                    浏览器悄悄清空成"没填"。提醒**只有一次性**，所以没有频度下拉。 */}
                <input
                  aria-describedby={
                    reminderFieldErrors.at
                      ? `${reminderFormId}-at-error`
                      : undefined
                  }
                  aria-invalid={reminderFieldErrors.at ? true : undefined}
                  disabled={reminderSubmitting}
                  id={`${reminderFormId}-at`}
                  inputMode="numeric"
                  onChange={(event) =>
                    updateReminderField("at", event.target.value)
                  }
                  placeholder="2026-09-20T15:00"
                  value={reminderValues.at}
                />
                {reminderFieldErrors.at ? (
                  <p
                    className="task-create-error"
                    id={`${reminderFormId}-at-error`}
                    role="alert"
                  >
                    {reminderFieldErrors.at}
                  </p>
                ) : null}

                <p className="task-create-hint">
                  {taskSource
                    ? "来源：当前会话的当前轮次。提醒只在填写的时刻触发一次；时间已过去时后端会如实拒绝。"
                    : "拿不到当前会话的当前轮次，暂时不能新建：提醒必须绑定来源会话与轮次。"}
                </p>

                <div className="task-create-actions">
                  <button
                    disabled={reminderSubmitting || !taskSource}
                    type="submit"
                  >
                    {reminderSubmitting ? "创建中…" : "创建提醒"}
                  </button>
                </div>

                {reminderError ? (
                  <p className="task-create-error" role="alert">
                    {reminderError}
                  </p>
                ) : null}
                {reminderNotice ? (
                  <p className="task-create-notice" role="status">
                    {reminderNotice}
                  </p>
                ) : null}
              </form>
            ) : null}
          </div>

          {/* 批次 D Task 1：编辑安排。入口在任务行菜单的「编辑」；这里预填现有的
              title / commitment / schedule，提交走 `PATCH /tasks/{id}`。 */}
          {editingTask && editValues ? (
            <form
              aria-label={`编辑安排《${editingTask.title}》`}
              className="task-create-form task-edit-form"
              id={`${editFormId}-form`}
              onSubmit={submitEdit}
            >
              <p className="task-create-hint">
                正在编辑《{editingTask.title}》。
              </p>

              <label htmlFor={`${editFormId}-title`}>任务标题</label>
              <input
                aria-describedby={
                  editFieldErrors.title ? `${editFormId}-title-error` : undefined
                }
                aria-invalid={editFieldErrors.title ? true : undefined}
                disabled={editSubmitting}
                id={`${editFormId}-title`}
                onChange={(event) => updateEditField("title", event.target.value)}
                value={editValues.title}
              />
              {editFieldErrors.title ? (
                <p
                  className="task-create-error"
                  id={`${editFormId}-title-error`}
                  role="alert"
                >
                  {editFieldErrors.title}
                </p>
              ) : null}

              <label htmlFor={`${editFormId}-commitment`}>到点执行的内容</label>
              <textarea
                aria-describedby={
                  editFieldErrors.commitment
                    ? `${editFormId}-commitment-error`
                    : undefined
                }
                aria-invalid={editFieldErrors.commitment ? true : undefined}
                disabled={editSubmitting}
                id={`${editFormId}-commitment`}
                onChange={(event) =>
                  updateEditField("commitment", event.target.value)
                }
                rows={3}
                value={editValues.commitment}
              />
              {editFieldErrors.commitment ? (
                <p
                  className="task-create-error"
                  id={`${editFormId}-commitment-error`}
                  role="alert"
                >
                  {editFieldErrors.commitment}
                </p>
              ) : null}

              <label htmlFor={`${editFormId}-kind`}>执行周期</label>
              <select
                disabled={editSubmitting}
                id={`${editFormId}-kind`}
                onChange={(event) => {
                  updateEditKind(event.target.value as TaskScheduleKind);
                }}
                value={editValues.kind}
              >
                {TASK_SCHEDULE_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {TASK_SCHEDULE_KIND_LABEL[kind]}
                  </option>
                ))}
              </select>

              <label htmlFor={`${editFormId}-time`}>时间（HH:MM，24 小时制）</label>
              <input
                aria-describedby={
                  editFieldErrors.time ? `${editFormId}-time-error` : undefined
                }
                aria-invalid={editFieldErrors.time ? true : undefined}
                disabled={editSubmitting}
                id={`${editFormId}-time`}
                inputMode="numeric"
                onChange={(event) => updateEditField("time", event.target.value)}
                value={editValues.time}
              />
              {editFieldErrors.time ? (
                <p
                  className="task-create-error"
                  id={`${editFormId}-time-error`}
                  role="alert"
                >
                  {editFieldErrors.time}
                </p>
              ) : null}

              {editValues.kind === "weekly" ? (
                <>
                  <label htmlFor={`${editFormId}-weekday`}>周几</label>
                  <select
                    aria-describedby={
                      editFieldErrors.weekday
                        ? `${editFormId}-weekday-error`
                        : undefined
                    }
                    aria-invalid={editFieldErrors.weekday ? true : undefined}
                    disabled={editSubmitting}
                    id={`${editFormId}-weekday`}
                    onChange={(event) =>
                      updateEditField("weekday", event.target.value)
                    }
                    value={editValues.weekday}
                  >
                    <option value="">请选择</option>
                    {TASK_WEEKDAY_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  {editFieldErrors.weekday ? (
                    <p
                      className="task-create-error"
                      id={`${editFormId}-weekday-error`}
                      role="alert"
                    >
                      {editFieldErrors.weekday}
                    </p>
                  ) : null}
                </>
              ) : null}

              {editValues.kind === "monthly" ? (
                <>
                  <label htmlFor={`${editFormId}-day`}>每月几号（1–28）</label>
                  <input
                    aria-describedby={
                      editFieldErrors.day ? `${editFormId}-day-error` : undefined
                    }
                    aria-invalid={editFieldErrors.day ? true : undefined}
                    disabled={editSubmitting}
                    id={`${editFormId}-day`}
                    inputMode="numeric"
                    onChange={(event) => updateEditField("day", event.target.value)}
                    type="number"
                    value={editValues.day}
                  />
                  {editFieldErrors.day ? (
                    <p
                      className="task-create-error"
                      id={`${editFormId}-day-error`}
                      role="alert"
                    >
                      {editFieldErrors.day}
                    </p>
                  ) : null}
                </>
              ) : null}

              {/* 如实写清楚"改期不会立刻补跑"（后端 R-D7：改期重置触发锚点）。
                  说不清就不假装——这句就是它的可见说明。 */}
              <p className="task-create-hint">
                改期不会立刻补跑：保存后新的时间从**下一次到点**开始生效；把时间改到
                已经过去的时刻也不会马上执行一次。
              </p>

              <div className="task-create-actions">
                <button disabled={editSubmitting} type="submit">
                  {editSubmitting ? "保存中…" : "保存"}
                </button>
                <button
                  disabled={editSubmitting}
                  onClick={closeTaskEditor}
                  type="button"
                >
                  取消
                </button>
              </div>

              {editError ? (
                <p className="task-create-error" role="alert">
                  {editError}
                </p>
              ) : null}
            </form>
          ) : null}

          {editNotice ? (
            <p className="task-create-notice" role="status">
              {editNotice}
            </p>
          ) : null}

          {actionError ? (
            <div className="proposal-error" role="alert">
              {actionError}
            </div>
          ) : null}
          {loading ? (
            <div aria-hidden="true" className="skeleton-panel">
              <span className="skeleton-line" />
              <span className="skeleton-line is-short" />
              <span className="skeleton-line" />
            </div>
          ) : null}
          {!loading && loadError ? (
            <EmptyState
              action={
                <button onClick={() => void load()} type="button">
                  重试
                </button>
              }
              desc={loadError}
              title="没加载出来"
            />
          ) : null}
          {!loading && !loadError && tasks.length === 0 ? (
            <EmptyState
              desc="可以点上面的「新建定时任务」自己建，也可以等助手在聊天中提出、你确认后生效。"
              title="还没有已安排的事项"
            />
          ) : null}
          {tasks.map((task) => {
            const runs = runsByTask[task.id] ?? [];
            const lastRun = runs.length ? runs[runs.length - 1] : null;
            const running = lastRun?.status === "running";
            const awaiting = lastRun?.awaitingUser ?? false;
            const lastRunLabel = awaiting
              ? "等待你处理"
              : lastRun
                ? RUN_STATUS_LABEL[lastRun.status]
                : "";
            const lastRunNote = awaiting
              ? (lastRun?.awaitingNote ?? null)
              : (lastRun?.error ?? null);
            const attemptSuffix =
              lastRun && lastRun.attempt > 1 ? ` · 第 ${lastRun.attempt} 次尝试` : "";
            // 切片 10 T4（item 4）：后端只在"这一轮可以重跑"时把 `retryable` 置真
            // （提醒恒假）。失败 + 可重试 → 主按钮变成"重试"，走既有重试端点；
            // 没有失败 run / 不可重试时不提供这个入口（按钮仍是"立即执行一次"）。
            const retryable =
              lastRun?.status === "failed" && lastRun.retryable === true;
            const startAction = retryable ? retryRun : runNow;
            const menuItems: RowMenuItem[] = [];
            // 批次 D Task 1：编辑入口（`PATCH /tasks/{id}`）。`list_tasks` 默认不返回
            // 已取消的行，所以这里能拿到的一定不是终态；后端对终态仍会回 409。
            menuItems.push({
              label: "编辑",
              disabled: busyTaskId === task.id,
              onSelect: () => openTaskEditor(task),
            });
            if (task.status === "active") {
              menuItems.push({
                label: "暂停",
                disabled: busyTaskId === task.id,
                onSelect: () => void changeStatus(task.id, "pause"),
              });
            }
            if (task.status !== "cancelled") {
              menuItems.push({
                label: "取消安排",
                disabled: busyTaskId === task.id,
                onSelect: () => requestCancel(task),
              });
            }
            menuItems.push({
              label: "查看会话",
              onSelect: () => onOpenConversation(task.sourceConversationId),
            });
            // 批次 B Task 1（ADR-1）：到点执行不再落进源会话，而是该任务**自己的
            // 执行记录会话**。这里给一个直达入口，让"可见"真的可被看见；旧数据
            // （或降级/失败路径）里最近一次 run 仍落在源会话时**不**渲染——两个
            // 入口指向同一个会话只会让用户以为有两份记录。
            if (
              lastRun &&
              lastRun.conversationId !== task.sourceConversationId
            ) {
              menuItems.push({
                label: "查看执行记录",
                onSelect: () => onOpenConversation(lastRun.conversationId),
              });
            }
            return (
              <div className="memory-item" key={task.id}>
                <div className="memory-content">
                  《{task.title}》 {task.scheduleDescription}
                  <span className="proposal-kind">{STATUS_LABEL[task.status]}</span>
                  {lastRun ? (
                    <span className="proposal-reason">
                      上次执行：{RUN_TRIGGER_LABEL[lastRun.trigger]} ·{" "}
                      {lastRunLabel}
                      {attemptSuffix}
                      {lastRunNote ? `（${lastRunNote}）` : ""}
                    </span>
                  ) : null}
                </div>
                <div className="memory-actions">
                  {awaiting ? (
                    <button
                      onClick={() =>
                        onOpenConversation(
                          lastRun?.conversationId ?? task.sourceConversationId,
                        )
                      }
                      type="button"
                    >
                      去处理
                    </button>
                  ) : task.status === "active" ? (
                    <button
                      disabled={busyTaskId === task.id || running}
                      onClick={() => void startAction(task.id)}
                      type="button"
                    >
                      {running
                        ? "执行中…"
                        : retryable
                          ? "重试"
                          : "立即执行一次"}
                    </button>
                  ) : task.status === "paused" ? (
                    <button
                      disabled={busyTaskId === task.id}
                      onClick={() => void changeStatus(task.id, "resume")}
                      type="button"
                    >
                      恢复
                    </button>
                  ) : null}
                  <RowMenu items={menuItems} />
                </div>
              </div>
            );
          })}
          {reminders.length > 0 ? (
            <h3 className="settings-section-title">一次性提醒</h3>
          ) : null}
          {reminders.map((reminder) => {
            const runs = runsByTask[reminder.id] ?? [];
            const lastRun = runs.length ? runs[runs.length - 1] : null;
            const awaiting = lastRun?.awaitingUser ?? false;
            const menuItems: RowMenuItem[] = [];
            if (reminder.status === "pending") {
              menuItems.push({
                label: "取消提醒",
                disabled: busyTaskId === reminder.id,
                onSelect: () =>
                  setPendingCancel({
                    kind: "reminder",
                    id: reminder.id,
                    title: reminder.title,
                  }),
              });
            }
            menuItems.push({
              label: "查看会话",
              onSelect: () => onOpenConversation(reminder.sourceConversationId),
            });
            return (
              <div className="memory-item" key={reminder.id}>
                <div className="memory-content">
                  《{reminder.title}》 {formatDue(reminder.dueAt)}
                  <span className="proposal-kind">
                    {REMINDER_STATUS_LABEL[reminder.status]}
                  </span>
                  {lastRun ? (
                    <span className="proposal-reason">
                      上次执行：{RUN_TRIGGER_LABEL[lastRun.trigger]} ·{" "}
                      {awaiting ? "等待你处理" : RUN_STATUS_LABEL[lastRun.status]}
                      {awaiting
                        ? lastRun.awaitingNote
                          ? `（${lastRun.awaitingNote}）`
                          : ""
                        : lastRun.error
                          ? `（${lastRun.error}）`
                          : ""}
                    </span>
                  ) : null}
                </div>
                <div className="memory-actions">
                  {/* 提醒**刻意**不提供"重试"：提醒没有重跑端点（`start_reminder`
                      只认 PENDING，仓储也没有 pending 复位），后端因此把提醒 run 的
                      `retryable` 恒置 false（T3 §8）。即使将来出现 true 的提醒 run，
                      这里也不会渲染重试按钮——这不是靠字段，而是按 subject 分工。 */}
                  {awaiting ? (
                    <button
                      onClick={() =>
                        onOpenConversation(
                          lastRun?.conversationId ?? reminder.sourceConversationId,
                        )
                      }
                      type="button"
                    >
                      去处理
                    </button>
                  ) : null}
                  <RowMenu items={menuItems} />
                </div>
              </div>
            );
          })}
      </div>

      {pendingCancel ? (
        <ConfirmDialog
          body={
            pendingCancel.kind === "task"
              ? `取消后，Assistant 将不再按时执行《${pendingCancel.title}》。`
              : `取消后，Assistant 将不再按时提醒你《${pendingCancel.title}》。`
          }
          confirmLabel={pendingCancel.kind === "task" ? "取消安排" : "取消提醒"}
          onClose={() => setPendingCancel(null)}
          onConfirm={confirmPendingCancel}
          title={pendingCancel.kind === "task" ? "取消安排" : "取消提醒"}
        />
      ) : null}
    </>
  );
}
