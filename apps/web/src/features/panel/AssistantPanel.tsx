import type { PendingProposal } from "../chat/apiTypes";
import { MemoryContent } from "../memory/MemoryManagement";
import { NotificationsContent } from "../notifications/NotificationsDrawer";
import { ScheduledTasksContent } from "../tasks/ScheduledTasks";

export type AssistantPanelTab = "notifications" | "scheduled" | "memory";

type AssistantPanelProps = {
  onClose: () => void;
  onOpenConversation: (conversationId: string) => void;
  onTabChange: (tab: AssistantPanelTab) => void;
  pendingProposals: PendingProposal[];
  tab: AssistantPanelTab;
  unreadCount: number;
};

export function AssistantPanel({
  onClose,
  onOpenConversation,
  onTabChange,
  pendingProposals,
  tab,
  unreadCount,
}: AssistantPanelProps) {
  return (
    <aside aria-label="助手面板" className="assistant-panel">
      <header className="assistant-panel-header">
        <div className="assistant-panel-tabs" role="tablist">
          <button
            aria-selected={tab === "notifications"}
            onClick={() => onTabChange("notifications")}
            role="tab"
            type="button"
          >
            通知{unreadCount > 0 ? `（${unreadCount}）` : ""}
          </button>
          <button
            aria-selected={tab === "scheduled"}
            onClick={() => onTabChange("scheduled")}
            role="tab"
            type="button"
          >
            已安排
          </button>
          <button
            aria-selected={tab === "memory"}
            onClick={() => onTabChange("memory")}
            role="tab"
            type="button"
          >
            记忆
          </button>
        </div>
        <button className="assistant-panel-close" onClick={onClose} type="button">
          关闭
        </button>
      </header>
      <div className="assistant-panel-body">
        {tab === "notifications" ? (
          <NotificationsContent
            onOpenConversation={onOpenConversation}
            pendingProposals={pendingProposals}
          />
        ) : tab === "scheduled" ? (
          <ScheduledTasksContent onOpenConversation={onOpenConversation} />
        ) : (
          <MemoryContent />
        )}
      </div>
    </aside>
  );
}
