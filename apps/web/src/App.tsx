import { useEffect, useMemo, useState } from "react";
import { WorkspacePanel } from "./features/artifacts/WorkspacePanel";
import { chatApi } from "./features/chat/api";
import type { PermissionMode, TaskNotification } from "./features/chat/apiTypes";
import { MemoryManagement } from "./features/memory/MemoryManagement";
import { NotificationsDrawer } from "./features/notifications/NotificationsDrawer";
import { ScheduledTasks } from "./features/tasks/ScheduledTasks";
import { SettingsOverlay } from "./features/settings/SettingsOverlay";
import { useWorkspace } from "./features/artifacts/useWorkspace";
import { ChatWorkSurface } from "./features/chat/ChatWorkSurface";
import { SessionRail } from "./features/chat/SessionRail";
import { useChatApplication } from "./features/chat/useChatApplication";
import { useProposals } from "./features/proposals/useProposals";

const TERMINAL_TURN_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function App() {
  const chat = useChatApplication();
  const proposals = useProposals(chat.activeConversationId);
  const [railOpen, setRailOpen] = useState(false);

  const latestTurn = chat.activeSnapshot?.turns.at(-1);
  const latestTurnStatus = latestTurn
    ? (chat.liveTurns[latestTurn.turn.id]?.status ?? latestTurn.turn.status)
    : undefined;

  useEffect(() => {
    if (!chat.activeConversationId || !latestTurn) return;
    if (!latestTurnStatus || !TERMINAL_TURN_STATUSES.has(latestTurnStatus)) return;
    proposals.watchTurn(chat.activeConversationId, latestTurn.turn.id);
  }, [chat.activeConversationId, latestTurn, latestTurnStatus, proposals.watchTurn]);

  const pendingArtifactProposalCount = useMemo(
    () =>
      proposals.artifactProposalsFor(chat.activeConversationId).filter(
        (item) => item.status === "pending",
      ).length,
    [proposals, chat.activeConversationId],
  );
  const workspace = useWorkspace(chat.activeConversationId, pendingArtifactProposalCount);
  const workspaceVisible = workspace.workspace?.visible === true;
  const [workspaceCollapsed, setWorkspaceCollapsed] = useState(false);
  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const [toasts, setToasts] = useState<TaskNotification[]>([]);
  const [scheduledOpen, setScheduledOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [permissionMode, setPermissionMode] = useState<PermissionMode | null>(null);

  useEffect(() => {
    chatApi
      .getPermissionSettings()
      .then((settings) => setPermissionMode(settings.mode))
      .catch(() => setPermissionMode(null));
  }, []);

  useEffect(() => {
    setWorkspaceCollapsed(false);
    setWorkspaceDrawerOpen(false);
  }, [chat.activeConversationId]);

  useEffect(() => {
    let cancelled = false;
    const known = new Set<string>();
    let seeded = false;
    const pushToast = (item: TaskNotification) => {
      setToasts((current) => [...current, item]);
      globalThis.setTimeout(() => {
        setToasts((current) => current.filter((toast) => toast.id !== item.id));
      }, 8000);
      if (
        document.hidden &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted" &&
        localStorage.getItem("endless-task-desktop-notifications") === "1"
      ) {
        try {
          new Notification(item.title, { body: item.body });
        } catch {
          // 桌面通知失败静默降级。
        }
      }
    };
    const poll = async () => {
      try {
        const items = await chatApi.listNotifications(true);
        if (cancelled) return;
        setUnreadCount(items.length);
        const fresh = items.filter((item) => !known.has(item.id));
        items.forEach((item) => known.add(item.id));
        if (!seeded) {
          seeded = true;
          return;
        }
        fresh.forEach(pushToast);
      } catch {
        // 通知轮询失败静默降级。
      }
    };
    void poll();
    const timer = globalThis.setInterval(() => void poll(), 10000);
    const refresh = () => void poll();
    globalThis.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      cancelled = true;
      globalThis.clearInterval(timer);
      globalThis.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  const dismissToast = async (item: TaskNotification) => {
    setToasts((current) => current.filter((toast) => toast.id !== item.id));
    try {
      await chatApi.markNotificationRead(item.id);
    } catch {
      // 标记已读失败不阻断跳转。
    }
    setNotificationsOpen(false);
    void chat.openConversation(item.conversationId);
  };

  return (
    <div
      className={`app-shell${
        workspaceVisible && !workspaceCollapsed ? " workspace-open" : ""
      }`}
    >
      <SessionRail
        activeConversationId={chat.activeConversationId}
        conversations={chat.conversations}
        health={chat.health}
        open={railOpen}
        search={chat.search}
        statusFilter={chat.statusFilter}
        onClose={() => setRailOpen(false)}
        onNewConversation={() => {
          void chat.newConversation();
          setRailOpen(false);
        }}
        onSearchChange={chat.setSearch}
        onSelectConversation={(conversationId) => {
          void chat.openConversation(conversationId);
          setRailOpen(false);
        }}
        onStatusFilterChange={chat.setStatusFilter}
      />
      <ChatWorkSurface
        conversation={chat.activeSnapshot}
        draft={chat.draft}
        error={chat.error}
        health={chat.health}
        isGenerating={chat.isGenerating}
        liveTurns={chat.liveTurns}
        loading={chat.loading}
        pendingAction={chat.pendingAction}
        onArchive={() => void chat.changeConversationStatus("archived")}
        onCancel={() => void chat.cancel()}
        onDelete={() => void chat.deleteConversation()}
        onDismissError={() => chat.setError(null)}
        onDraftChange={chat.setDraft}
        onMenu={() => setRailOpen(true)}
        onOpenMemory={() => setMemoryOpen(true)}
        onOpenNotifications={() => setNotificationsOpen(true)}
        unreadNotificationCount={unreadCount}
        onOpenScheduled={() => setScheduledOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
        permissionMode={permissionMode}
        onRegenerate={(turnId) => void chat.regenerate(turnId)}
        onResolveApproval={(turnId, approvalId, decision) =>
          void chat.resolveApproval(turnId, approvalId, decision)
        }
        onRemoveFile={(fileId) => void chat.removeFile(fileId)}
        onRename={(title) => void chat.renameConversation(title)}
        onRestore={() => void chat.changeConversationStatus("active")}
        onRetry={(turnId) => void chat.retry(turnId)}
        onSelectVariant={(turnId, variantId) =>
          void chat.selectVariant(turnId, variantId)
        }
        onSend={() => void chat.send()}
        onUploadFile={(file) => void chat.uploadFile(file)}
        proposalBusyId={proposals.busyProposalId}
        proposalErrors={proposals.resolveErrors}
        resolvedArtifacts={proposals.resolvedArtifacts}
        turnProposals={(turnId) =>
          proposals.forTurn(chat.activeConversationId, turnId)
        }
        onResolveArtifactProposal={(proposalId, decision) =>
          void proposals.resolveArtifactProposal(proposalId, decision)
        }
        onResolveMemoryProposal={(proposalId, decision) =>
          void proposals.resolveMemoryProposal(proposalId, decision)
        }
        onResolveTaskProposal={(proposalId, decision) =>
          void proposals.resolveTaskProposal(proposalId, decision)
        }
      />
      {workspaceVisible && (!workspaceCollapsed || workspaceDrawerOpen) ? (
        <WorkspacePanel
          conversationId={workspace.workspace!.conversationId}
          drawerOpen={workspaceDrawerOpen}
          latestTurnId={latestTurn?.turn.id ?? null}
          onCollapse={() => {
            setWorkspaceCollapsed(true);
            setWorkspaceDrawerOpen(false);
          }}
          onWorkspaceRefresh={() => void workspace.refresh()}
          workspace={workspace.workspace!}
        />
      ) : null}
      {workspaceVisible && workspaceCollapsed && !workspaceDrawerOpen ? (
        <button
          className="workspace-reopen"
          onClick={() => setWorkspaceCollapsed(false)}
          type="button"
        >
          工作区
        </button>
      ) : null}
      {workspaceVisible ? (
        <button
          className="workspace-fab"
          onClick={() => setWorkspaceDrawerOpen((current) => !current)}
          type="button"
        >
          工作区
        </button>
      ) : null}
      {workspaceVisible && workspaceDrawerOpen ? (
        <button
          aria-label="关闭工作区"
          className="workspace-scrim"
          onClick={() => setWorkspaceDrawerOpen(false)}
          type="button"
        />
      ) : null}
      {railOpen ? (
        <button
          aria-label="关闭会话列表"
          className="rail-scrim"
          onClick={() => setRailOpen(false)}
          type="button"
        />
      ) : null}
      {memoryOpen ? (
        <MemoryManagement onClose={() => setMemoryOpen(false)} />
      ) : null}
      {notificationsOpen ? (
        <NotificationsDrawer
          onClose={() => setNotificationsOpen(false)}
          onOpenConversation={(conversationId) => {
            setNotificationsOpen(false);
            void chat.openConversation(conversationId);
          }}
        />
      ) : null}
      {scheduledOpen ? (
        <ScheduledTasks
          onClose={() => setScheduledOpen(false)}
          onOpenConversation={(conversationId) => {
            setScheduledOpen(false);
            void chat.openConversation(conversationId);
          }}
        />
      ) : null}
      <div className="toast-stack">
        {toasts.map((item) => (
          <button
            className="toast"
            key={item.id}
            onClick={() => void dismissToast(item)}
            type="button"
          >
            <span className="toast-title">{item.title}</span>
            <span className="toast-body">{item.body}</span>
          </button>
        ))}
      </div>
      {settingsOpen ? (
        <SettingsOverlay
          onClose={() => setSettingsOpen(false)}
          onModeChanged={setPermissionMode}
        />
      ) : null}
    </div>
  );
}
