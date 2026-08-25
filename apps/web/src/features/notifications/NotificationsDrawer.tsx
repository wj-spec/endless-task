import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { TaskNotification } from "../chat/apiTypes";

type NotificationsDrawerProps = {
  onClose: () => void;
  onOpenConversation: (conversationId: string) => void;
};

const KIND_LABEL: Record<TaskNotification["kind"], string> = {
  run_completed: "完成",
  run_failed: "失败",
  run_awaiting: "等待你处理",
};

export function NotificationsDrawer({
  onClose,
  onOpenConversation,
}: NotificationsDrawerProps) {
  const [items, setItems] = useState<TaskNotification[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await chatApi.listNotifications(false));
      setLoadError(null);
    } catch {
      setLoadError("无法加载通知，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const open = async (item: TaskNotification) => {
    setBusyId(item.id);
    try {
      if (!item.readAt) {
        await chatApi.markNotificationRead(item.id);
      }
    } catch {
      // 标记已读失败不阻断跳转。
    } finally {
      setBusyId(null);
    }
    onOpenConversation(item.conversationId);
  };

  const markAll = async () => {
    setBusyId("all");
    try {
      await chatApi.markAllNotificationsRead();
      await load();
    } catch {
      setLoadError("操作失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div aria-label="通知" className="overlay" role="dialog">
      <div className="overlay-panel">
        <header className="overlay-header">
          <h2>通知</h2>
          <button onClick={() => void markAll()} type="button">
            全部已读
          </button>
          <button onClick={onClose} type="button">
            关闭
          </button>
        </header>
        <div className="overlay-body">
          {loading ? <div className="overlay-empty">正在加载…</div> : null}
          {!loading && loadError ? (
            <div className="overlay-empty">{loadError}</div>
          ) : null}
          {!loading && !loadError && items.length === 0 ? (
            <div className="overlay-empty">
              暂时没有通知；任务执行完成后会在这里告诉你。
            </div>
          ) : null}
          {items.map((item) => (
            <div className="memory-item" key={item.id}>
              <div className="memory-content">
                {!item.readAt ? <span className="unread-dot" /> : null}
                <strong>{item.title}</strong>
                <span className="proposal-kind">{KIND_LABEL[item.kind]}</span>
                <span className="proposal-reason">{item.body}</span>
              </div>
              <div className="memory-actions">
                <button
                  disabled={busyId !== null}
                  onClick={() => void open(item)}
                  type="button"
                >
                  {item.kind === "run_awaiting" ? "去处理" : "查看会话"}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
