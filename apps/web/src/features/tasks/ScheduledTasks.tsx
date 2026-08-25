import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "../chat/api";
import type { TaskRun, TaskSummary } from "../chat/apiTypes";

type ScheduledTasksProps = {
  onClose: () => void;
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

const POLL_DELAYS_MS = [2000, 6000, 12000];

export function ScheduledTasks({ onClose, onOpenConversation }: ScheduledTasksProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [runsByTask, setRunsByTask] = useState<Record<string, TaskRun[]>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
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
      setLoadError(null);
      await Promise.all(items.map((task) => loadRuns(task.id)));
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

  const requestCancel = (taskId: string) => {
    if (
      !globalThis.confirm("取消这条安排？Assistant 将不再按时执行它。")
    ) {
      return;
    }
    void changeStatus(taskId, "cancel");
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
    <div aria-label="已安排" className="overlay" role="dialog">
      <div className="overlay-panel">
        <header className="overlay-header">
          <h2>已安排</h2>
          <button onClick={onClose} type="button">
            关闭
          </button>
        </header>
        <div className="overlay-body">
          {actionError ? (
            <div className="proposal-error" role="alert">
              {actionError}
            </div>
          ) : null}
          {loading ? <div className="overlay-empty">正在加载…</div> : null}
          {!loading && loadError ? (
            <div className="overlay-empty">{loadError}</div>
          ) : null}
          {!loading && !loadError && tasks.length === 0 ? (
            <div className="overlay-empty">
              还没有已安排的事项；需要时 Assistant 会在聊天中提出。
            </div>
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
            return (
              <div className="memory-item" key={task.id}>
                <div className="memory-content">
                  《{task.title}》 {task.scheduleDescription}
                  <span className="proposal-kind">{STATUS_LABEL[task.status]}</span>
                </div>
                <div className="memory-actions">
                  {lastRun ? (
                    <span className="proposal-reason">
                      上次执行：{RUN_TRIGGER_LABEL[lastRun.trigger]} ·{" "}
                      {lastRunLabel}
                      {lastRunNote ? `（${lastRunNote}）` : ""}
                    </span>
                  ) : null}
                  {task.status === "active" ? (
                    <button
                      disabled={busyTaskId === task.id || running}
                      onClick={() => void runNow(task.id)}
                      type="button"
                    >
                      {running ? "执行中…" : "立即执行一次"}
                    </button>
                  ) : null}
                  {task.status === "active" ? (
                    <button
                      disabled={busyTaskId === task.id}
                      onClick={() => void changeStatus(task.id, "pause")}
                      type="button"
                    >
                      暂停
                    </button>
                  ) : null}
                  {task.status === "paused" ? (
                    <button
                      disabled={busyTaskId === task.id}
                      onClick={() => void changeStatus(task.id, "resume")}
                      type="button"
                    >
                      恢复
                    </button>
                  ) : null}
                  <button
                    disabled={busyTaskId === task.id}
                    onClick={() => requestCancel(task.id)}
                    type="button"
                  >
                    取消安排
                  </button>
                  <button
                    onClick={() => onOpenConversation(task.sourceConversationId)}
                    type="button"
                  >
                    {awaiting ? "去处理" : "查看会话"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
