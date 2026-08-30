import type {
  CapabilitySnapshot,
  CapabilityState,
  PendingProposal,
  Workspace,
} from "../chat/apiTypes";
import { KnowledgeContent } from "../knowledge/KnowledgeManagement";
import { MemoryContent } from "../memory/MemoryManagement";
import { NotificationsContent } from "../notifications/NotificationsDrawer";
import { ScheduledTasksContent } from "../tasks/ScheduledTasks";
import { SkillsContent } from "../skills/SkillsManagement";
import { McpContent } from "../mcp/McpManagement";
import { ProviderManagement } from "../providers/ProviderManagement";
import { RuntimeV2Panel } from "./RuntimeV2Panel";

export type AssistantPanelTab =
  | "notifications"
  | "scheduled"
  | "memory"
  | "knowledge"
  | "skills"
  | "mcp"
  | "providers"
  | "runtime-v2";

type AssistantPanelScope = "tasks" | "knowledge" | "system";

type AssistantPanelTabItem = {
  id: AssistantPanelTab;
  label: string;
  scope: AssistantPanelScope;
};

const PANEL_TABS: AssistantPanelTabItem[] = [
  { id: "notifications", label: "通知", scope: "tasks" },
  { id: "scheduled", label: "已安排", scope: "tasks" },
  { id: "memory", label: "记忆", scope: "knowledge" },
  { id: "knowledge", label: "知识", scope: "knowledge" },
  { id: "skills", label: "技能", scope: "system" },
  { id: "mcp", label: "MCP", scope: "system" },
  { id: "providers", label: "模型", scope: "system" },
  { id: "runtime-v2", label: "执行状态", scope: "system" },
];

const TAB_SCOPES = Object.fromEntries(
  PANEL_TABS.map((item) => [item.id, item.scope]),
) as Record<AssistantPanelTab, AssistantPanelScope>;

const SCOPE_LABELS: Record<AssistantPanelScope, string> = {
  tasks: "任务与通知",
  knowledge: "知识与记忆",
  system: "系统能力",
};

type AssistantPanelProps = {
  capabilities: CapabilitySnapshot | null;
  conversationId: string | null;
  onCapabilitiesChanged: () => void | Promise<void>;
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
  capabilities,
  conversationId,
  onCapabilitiesChanged,
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
  const panelScope = TAB_SCOPES[tab];
  const visibleTabs = PANEL_TABS.filter((item) => item.scope === panelScope);
  const capabilityItems: Array<{
    label: string;
    state: CapabilityState | "loading";
    tab: AssistantPanelTab;
  }> = [
    { label: "模型", state: capabilities?.provider.state ?? "loading", tab: "providers" },
    { label: "MCP", state: capabilities?.mcp.state ?? "loading", tab: "mcp" },
    { label: "技能", state: capabilities?.skills.state ?? "loading", tab: "skills" },
  ];

  return (
    <aside aria-label={`${SCOPE_LABELS[panelScope]}面板`} className="assistant-panel">
      <header className="assistant-panel-header">
        <div
          aria-label={`${SCOPE_LABELS[panelScope]}功能`}
          className="assistant-panel-tabs"
          role="tablist"
        >
          {visibleTabs.map((item) => (
            <button
              aria-selected={tab === item.id}
              key={item.id}
              onClick={() => onTabChange(item.id)}
              role="tab"
              type="button"
            >
              {item.label}
              {item.id === "notifications" && unreadCount > 0
                ? `（${unreadCount}）`
                : ""}
            </button>
          ))}
        </div>
        <button
          aria-label={`关闭${SCOPE_LABELS[panelScope]}面板`}
          className="assistant-panel-close"
          onClick={onClose}
          type="button"
        >
          <span aria-hidden="true">×</span>
        </button>
      </header>
      {panelScope === "system" ? (
        <div aria-label="能力健康" className="capability-strip">
          {capabilityItems.map((item) => (
            <button
              className={`capability-pill is-${item.state}`}
              key={item.label}
              onClick={() => onTabChange(item.tab)}
              type="button"
            >
              <span aria-hidden="true" className="capability-dot" />
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
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
          <SkillsContent
            onChanged={onCapabilitiesChanged}
            workspaceId={workspaceId}
          />
        ) : tab === "mcp" ? (
          <McpContent onChanged={onCapabilitiesChanged} />
        ) : tab === "runtime-v2" ? (
          <RuntimeV2Panel conversationId={conversationId} />
        ) : (
          <ProviderManagement onChanged={onProvidersChanged} />
        )}
      </div>
    </aside>
  );
}
