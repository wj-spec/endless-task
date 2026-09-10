import { useId, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type {
  CapabilitySnapshot,
  CapabilityState,
  PendingProposal,
  RuntimeV2Lane,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  Workspace,
} from "../chat/apiTypes";
import type { RuntimeConnectionPhase } from "../chat/runtimeController";
import { KnowledgeContent } from "../knowledge/KnowledgeManagement";
import { MemoryContent } from "../memory/MemoryManagement";
import { NotificationsContent } from "../notifications/NotificationsDrawer";
import { ScheduledTasksContent } from "../tasks/ScheduledTasks";
import { SkillsContent } from "../skills/SkillsManagement";
import { McpContent } from "../mcp/McpManagement";
import { ProviderManagement } from "../providers/ProviderManagement";
import { RuntimeV2Panel } from "./RuntimeV2Panel";
import { CloseIcon } from "../ui/Icons";
import { useMediaQuery } from "../ui/useMediaQuery";
import { useModalDialog } from "../ui/useModalDialog";

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
  { id: "providers", label: "模型服务", scope: "system" },
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
  onOpenConversation: (conversationId: string, focusTurnId?: string | null) => void;
  /** S7：把技能注入当前会话输入框。 */
  onInjectSkill?: (name: string) => void;
  onTabChange: (tab: AssistantPanelTab) => void;
  pendingProposals: PendingProposal[];
  onProvidersChanged: () => void | Promise<void>;
  runtimeConnection: {
    phase: RuntimeConnectionPhase;
    error: string | null;
  } | null;
  runtimeEvents: RuntimeV2ProductEvent[];
  runtimeLanes: RuntimeV2Lane[];
  runtimeSnapshot: RuntimeV2Snapshot | null;
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
  onInjectSkill,
  onTabChange,
  pendingProposals,
  onProvidersChanged,
  runtimeConnection,
  runtimeEvents,
  runtimeLanes,
  runtimeSnapshot,
  tab,
  unreadCount,
  workspaceId,
  workspaces,
}: AssistantPanelProps) {
  const panelId = useId();
  const selectedTabRef = useRef<HTMLButtonElement>(null);
  const isMobile = useMediaQuery("(max-width: 760px)");
  const isDialog = isMobile;
  const dialogRef = useModalDialog<HTMLElement>({
    active: isDialog,
    initialFocusRef: selectedTabRef,
    onClose,
  });

  const panelScope = TAB_SCOPES[tab];
  const visibleTabs = PANEL_TABS.filter((item) => item.scope === panelScope);
  const handleTabKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    const tabs = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    );
    if (!tabs.length) return;
    const currentIndex = tabs.findIndex((item) => item === document.activeElement);
    let nextIndex = currentIndex < 0 ? 0 : currentIndex;
    if (event.key === "ArrowLeft") {
      nextIndex = (nextIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "ArrowRight") {
      nextIndex = (nextIndex + 1) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    event.preventDefault();
    tabs[nextIndex]?.focus();
    const nextTab = visibleTabs[nextIndex];
    if (nextTab) onTabChange(nextTab.id);
  };
  const capabilityItems: Array<{
    label: string;
    state: CapabilityState | "loading";
    tab: AssistantPanelTab;
  }> = [
    { label: "模型服务", state: capabilities?.provider.state ?? "loading", tab: "providers" },
    { label: "MCP", state: capabilities?.mcp.state ?? "loading", tab: "mcp" },
    { label: "技能", state: capabilities?.skills.state ?? "loading", tab: "skills" },
  ];

  return (
    <aside
      aria-label={`${SCOPE_LABELS[panelScope]}面板`}
      aria-modal={isDialog ? true : undefined}
      className="assistant-panel"
      ref={dialogRef}
      role={isDialog ? "dialog" : undefined}
    >
      <header className="assistant-panel-header">
        <div
          aria-label={`${SCOPE_LABELS[panelScope]}功能`}
          className="assistant-panel-tabs"
          onKeyDown={handleTabKeyDown}
          role="tablist"
        >
          {visibleTabs.map((item) => (
            <button
              aria-controls={`${panelId}-content`}
              aria-selected={tab === item.id}
              id={`${panelId}-${item.id}`}
              key={item.id}
              onClick={() => onTabChange(item.id)}
              ref={tab === item.id ? selectedTabRef : undefined}
              role="tab"
              tabIndex={tab === item.id ? 0 : -1}
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
          <CloseIcon size={20} />
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
      <div
        aria-labelledby={`${panelId}-${tab}`}
        className="assistant-panel-body"
        id={`${panelId}-content`}
        role="tabpanel"
        tabIndex={0}
      >
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
            onInjectSkill={onInjectSkill}
            onChanged={onCapabilitiesChanged}
            workspaceId={workspaceId}
          />
        ) : tab === "mcp" ? (
          <McpContent onChanged={onCapabilitiesChanged} />
        ) : tab === "runtime-v2" ? (
          <RuntimeV2Panel
            connection={runtimeConnection}
            conversationId={conversationId}
            events={runtimeEvents}
            lanes={runtimeLanes}
            snapshot={runtimeSnapshot}
            delegation={capabilities?.delegation ?? null}
          />
        ) : (
          <ProviderManagement onChanged={onProvidersChanged} />
        )}
      </div>
    </aside>
  );
}
