import type { PendingProposal, Workspace } from "../chat/apiTypes";
import { KnowledgeContent } from "../knowledge/KnowledgeManagement";
import { MemoryContent } from "../memory/MemoryManagement";
import { NotificationsContent } from "../notifications/NotificationsDrawer";
import { ScheduledTasksContent } from "../tasks/ScheduledTasks";
import { SkillsContent } from "../skills/SkillsManagement";
import { McpContent } from "../mcp/McpManagement";
import { ProviderManagement } from "../providers/ProviderManagement";

export type AssistantPanelTab =
  | "notifications"
  | "scheduled"
  | "memory"
  | "knowledge"
  | "skills"
  | "mcp"
  | "providers";

type AssistantPanelProps = {
  onClose: () => void;
  onOpenConversation: (conversationId: string) => void;
  onTabChange: (tab: AssistantPanelTab) => void;
  pendingProposals: PendingProposal[];
  onProvidersChanged: () => void | Promise<void>;
  tab: AssistantPanelTab;
  unreadCount: number;
  workspaceId: string | null;
  workspaces: Workspace[];
};

export function AssistantPanel({
  onClose,
  onOpenConversation,
  onTabChange,
  pendingProposals,
  onProvidersChanged,
  tab,
  unreadCount,
  workspaceId,
  workspaces,
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
          <button
            aria-selected={tab === "knowledge"}
            onClick={() => onTabChange("knowledge")}
            role="tab"
            type="button"
          >
            知识
          </button>
          <button
            aria-selected={tab === "skills"}
            onClick={() => onTabChange("skills")}
            role="tab"
            type="button"
          >
            技能
          </button>
          <button
            aria-selected={tab === "mcp"}
            onClick={() => onTabChange("mcp")}
            role="tab"
            type="button"
          >
            MCP
          </button>
          <button
            aria-selected={tab === "providers"}
            onClick={() => onTabChange("providers")}
            role="tab"
            type="button"
          >
            模型
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
        ) : tab === "memory" ? (
          <MemoryContent onOpenConversation={onOpenConversation} />
        ) : tab === "knowledge" ? (
          <KnowledgeContent workspaceId={workspaceId} workspaces={workspaces} />
        ) : tab === "skills" ? (
          <SkillsContent workspaceId={workspaceId} />
        ) : tab === "mcp" ? (
          <McpContent />
        ) : (
          <ProviderManagement onChanged={onProvidersChanged} />
        )}
      </div>
    </aside>
  );
}
