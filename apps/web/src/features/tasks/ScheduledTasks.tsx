import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { TaskSummary } from "../chat/apiTypes";

type ScheduledTasksProps = {
  onClose: () => void;
};

const STATUS_LABEL: Record<TaskSummary["status"], string> = {
  active: "进行中",
  paused: "已暂停",
  cancelled: "已取消",
};

export function ScheduledTasks({ onClose }: ScheduledTasksProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setTasks(await chatApi.listTasks());
      setLoadError(null);
    } catch {
      setLoadError("无法加载已安排的事项，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

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
          {loading ? <div className="overlay-empty">正在加载…</div> : null}
          {!loading && loadError ? (
            <div className="overlay-empty">{loadError}</div>
          ) : null}
          {!loading && !loadError && tasks.length === 0 ? (
            <div className="overlay-empty">
              还没有已安排的事项；需要时 Assistant 会在聊天中提出。
            </div>
          ) : null}
          {tasks.map((task) => (
            <div className="memory-item" key={task.id}>
              <div className="memory-content">
                《{task.title}》 {task.scheduleDescription}
              </div>
              <span className="proposal-kind">{STATUS_LABEL[task.status]}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
