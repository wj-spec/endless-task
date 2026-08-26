import { useCallback, useEffect, useRef, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { chatApi } from "../chat/api";
import type { Reminder, TaskRun, TaskSummary } from "../chat/apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { RowMenu } from "../ui/RowMenu";
import type { RowMenuItem } from "../ui/RowMenu";

type ScheduledTasksContentProps = {
  onOpenConversation: (conversationId: string) => void;
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
}: ScheduledTasksContentProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [runsByTask, setRunsByTask] = useState<Record<string, TaskRun[]>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingCancel, setPendingCancel] = useState<PendingCancel | null>(null);
  const timers = useRef<number[]>([]);

  const loadRuns = useCallback(async (taskId: string) => {
    try {
      const runs = await chatApi.listTaskRuns(taskId);
      setRunsByTask((current) => ({ ...current, [taskId]: runs }));
    } catch {
      // 执行记录是增强信息，失败时静默降级。
    }
  }, []);

  const load = useCallback(async () => {
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
      setLoadError("无法加载已安排的事项，请重试。");
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
    } catch {
      setActionError("操作失败，请重试。");
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
    } catch {
      setActionError("操作失败，请重试。");
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
    } catch {
      setActionError("触发执行失败，请重试。");
    } finally {
      setBusyTaskId(null);
    }
  };

  return (
    <>
      <div className="panel-content">
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
              desc="需要时助手会在聊天中提出，你确认后才生效。"
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
            const menuItems: RowMenuItem[] = [];
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
                      onClick={() => void runNow(task.id)}
                      type="button"
                    >
                      {running ? "执行中…" : "立即执行一次"}
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
