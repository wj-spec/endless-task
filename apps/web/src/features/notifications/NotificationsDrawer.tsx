import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { PendingProposal, TaskNotification } from "../chat/apiTypes";
import { EmptyState } from "../ui/EmptyState";

type NotificationsContentProps = {
  /** 打开会话；可选 focusTurnId 用于滚动定位到触发通知的那一轮。 */
  onOpenConversation: (conversationId: string, focusTurnId?: string | null) => void;
  pendingProposals: PendingProposal[];
};

const KIND_LABEL: Record<TaskNotification["kind"], string> = {
  run_completed: "完成",
  run_failed: "失败",
  run_awaiting: "等待你处理",
};

const PROPOSAL_KIND_LABEL: Record<PendingProposal["kind"], string> = {
  artifact: "文档",
  task: "安排",
  knowledge: "知识",
  memory: "记忆",
};

export function NotificationsContent({
  onOpenConversation,
  pendingProposals,
}: NotificationsContentProps) {
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
    onOpenConversation(item.conversationId, item.turnId ?? item.runId);
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
    <div className="panel-content">
      <div className="panel-toolbar">
        <button disabled={busyId !== null} onClick={() => void markAll()} type="button">
          全部已读
        </button>
      </div>
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
      {!loading && !loadError && pendingProposals.length > 0 ? (
        <div className="pending-group">
          <h3>待确认提案（{pendingProposals.length}）</h3>
          {pendingProposals.map((proposal) => (
            <button
              className="pending-item"
              key={proposal.id}
              onClick={() => onOpenConversation(proposal.conversationId)}
              type="button"
            >
              <span className="proposal-kind">{PROPOSAL_KIND_LABEL[proposal.kind]}</span>
              <strong>{proposal.title}</strong>
            </button>
          ))}
        </div>
      ) : null}
      {!loading && !loadError && items.length === 0 ? (
        <EmptyState desc="任务执行完成后会在这里告诉你。" title="暂时没有通知" />
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
  );
}
