import { useCallback, useEffect, useMemo, useState } from "react";
import { WorkspacePanel } from "./features/artifacts/WorkspacePanel";
import { chatApi } from "./features/chat/api";
import type { PermissionMode, TaskNotification } from "./features/chat/apiTypes";
import { AssistantPanel } from "./features/panel/AssistantPanel";
import type { AssistantPanelTab } from "./features/panel/AssistantPanel";
import { CornerHub } from "./features/hub/CornerHub";
import { useAssistantHub } from "./features/hub/useAssistantHub";
import { SettingsOverlay } from "./features/settings/SettingsOverlay";
import { useWorkspace } from "./features/artifacts/useWorkspace";
import { ChatWorkSurface } from "./features/chat/ChatWorkSurface";
import { SessionRail } from "./features/chat/SessionRail";
import { useChatApplication } from "./features/chat/useChatApplication";
import { useProposals } from "./features/proposals/useProposals";

const TERMINAL_TURN_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function App() {
  const chat = useChatApplication();
  const proposals = useProposals(
    chat.activeConversationId,
    chat.sideConversationId,
  );
  const [railOpen, setRailOpen] = useState(false);
  const [railPreferredCollapsed, setRailPreferredCollapsed] = useState(
    () => window.innerWidth < 1180,
  );

  const latestTurn = chat.activeSnapshot?.turns.at(-1);
  const sideLatestTurn = chat.sideSnapshot?.turns.at(-1);

  useEffect(() => {
    const surfaces = [
      { conversationId: chat.activeConversationId, turn: latestTurn },
      { conversationId: chat.sideConversationId, turn: sideLatestTurn },
    ];
    for (const { conversationId, turn } of surfaces) {
      if (!conversationId || !turn) continue;
      if (turn.turn.conversationId !== conversationId) continue;
      const status = chat.liveTurns[turn.turn.id]?.status ?? turn.turn.status;
      if (!status || !TERMINAL_TURN_STATUSES.has(status)) continue;
      proposals.watchTurn(conversationId, turn.turn.id);
    }
  }, [
    chat.activeConversationId,
    chat.sideConversationId,
    chat.liveTurns,
    latestTurn,
    sideLatestTurn,
    proposals.watchTurn,
  ]);

  useEffect(() => {
    if (!chat.sideConversationId) return;
    setWorkspaceCollapsed(true);
    setWorkspaceDrawerOpen(false);
    setAssistantOpen(false);
  }, [chat.sideConversationId]);

  useEffect(() => {
    if (!chat.sideConversationId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (document.querySelector("[role='dialog']")) return;
      chat.closeSideConversation();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [chat.sideConversationId, chat.closeSideConversation]);

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

  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth < 1180) setRailPreferredCollapsed(true);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const crowdPanelOpen =
    !!chat.sideConversationId || (workspaceVisible && !workspaceCollapsed);
  const railCollapsed = crowdPanelOpen || railPreferredCollapsed;

  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [assistantTab, setAssistantTab] = useState<AssistantPanelTab>("notifications");
  const [toasts, setToasts] = useState<TaskNotification[]>([]);
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

  const pushToast = useCallback((item: TaskNotification) => {
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
  }, []);

  const hub = useAssistantHub(
    useCallback(
      (items: TaskNotification[]) => items.forEach(pushToast),
      [pushToast],
    ),
  );

  const openAssistantPanel = () => {
    setAssistantOpen(true);
    setWorkspaceCollapsed(true);
    setWorkspaceDrawerOpen(false);
  };

  const openWorkspacePanel = () => {
    chat.closeSideConversation();
    setAssistantOpen(false);
    setWorkspaceCollapsed(false);
    setWorkspaceDrawerOpen(true);
  };

  const dismissToast = async (item: TaskNotification) => {
    setToasts((current) => current.filter((toast) => toast.id !== item.id));
    try {
      await chatApi.markNotificationRead(item.id);
    } catch {
      // 标记已读失败不阻断跳转。
    }
    void chat.openConversation(item.conversationId);
  };

  return (
    <div
      className={`app-shell${
        workspaceVisible && !workspaceCollapsed ? " workspace-open" : ""
      }${chat.sideConversationId ? " side-open" : ""}${
        railCollapsed ? " rail-collapsed" : ""
      }`}
    >
      <SessionRail
        activeConversationId={chat.activeConversationId}
        conversations={chat.conversations}
        open={railOpen}
        search={chat.search}
        sideConversationId={chat.sideConversationId}
        statusFilter={chat.statusFilter}
        onClose={() => {
          setRailOpen(false);
          setRailPreferredCollapsed(true);
        }}
        onNewConversation={() => {
          void chat.newConversation();
          setRailOpen(false);
        }}
        onSearchChange={chat.setSearch}
        onSelectBranch={(branchId, parentId) => {
          void chat.openBranchInSide(branchId, parentId);
          setRailOpen(false);
        }}
        onSelectConversation={(conversationId) => {
          void chat.openConversation(conversationId);
          setRailOpen(false);
        }}
        onStatusFilterChange={chat.setStatusFilter}
        onChangeConversationStatus={(conversationId, status) =>
          void chat.changeConversationStatus(status, conversationId)
        }
        onDeleteConversation={(conversationId) =>
          void chat.deleteConversation(conversationId)
        }
        onPromoteConversation={(conversationId) => {
          void chat.promoteConversation(conversationId);
          setRailOpen(false);
        }}
        onRenameConversation={(conversationId, title) =>
          void chat.renameConversation(title, conversationId)
        }
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
        onCreateBranch={(forkTurnId) => void chat.createBranch(forkTurnId)}
        onDelete={() => void chat.deleteConversation()}
        onDismissError={() => chat.setError(null)}
        onDraftChange={chat.setDraft}
        onMenu={() => {
          if (window.innerWidth <= 760) setRailOpen(true);
          else setRailPreferredCollapsed(false);
        }}
        onPromote={() => void chat.promoteConversation()}
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
          void proposals
            .resolveArtifactProposal(proposalId, decision)
            .then(hub.refresh)
        }
        onResolveKnowledgeProposal={(proposalId, decision) =>
          void proposals
            .resolveKnowledgeProposal(proposalId, decision)
            .then(hub.refresh)
        }
        onResolveMemoryProposal={(proposalId, decision) =>
          void proposals
            .resolveMemoryProposal(proposalId, decision)
            .then(hub.refresh)
        }
        onResolveTaskProposal={(proposalId, decision) =>
          void proposals.resolveTaskProposal(proposalId, decision).then(hub.refresh)
        }
      />
      {chat.sideConversationId ? (
        <section aria-label="临时会话" className="side-chat-panel">
          <ChatWorkSurface
            conversation={chat.sideSnapshot}
            draft={chat.sideDraft}
            error={chat.error}
            health={chat.health}
            isGenerating={chat.sideIsGenerating}
            liveTurns={chat.liveTurns}
            loading={chat.sideLoading}
            pendingAction={chat.pendingAction}
            variant="side"
            onArchive={() => {}}
            onCancel={() => void chat.cancel(chat.sideConversationId ?? undefined)}
            onCloseSide={chat.closeSideConversation}
            onCreateBranch={() => {}}
            onDelete={() => {}}
            onDismissError={() => chat.setError(null)}
            onDraftChange={chat.setSideDraft}
            onMenu={() => {}}
            onPromote={() =>
              void chat.promoteConversation(chat.sideConversationId ?? undefined)
            }
            onRegenerate={(turnId) => void chat.regenerate(turnId)}
            onResolveApproval={(turnId, approvalId, decision) =>
              void chat.resolveApproval(turnId, approvalId, decision)
            }
            onRemoveFile={() => {}}
            onRename={() => {}}
            onRestore={() => {}}
            onRetry={(turnId) => void chat.retry(turnId)}
            onSelectVariant={(turnId, variantId) =>
              void chat.selectVariant(
                turnId,
                variantId,
                chat.sideConversationId ?? undefined,
              )
            }
            onSend={() => void chat.sendSide()}
            onUploadFile={() => {}}
            proposalBusyId={proposals.busyProposalId}
            proposalErrors={proposals.resolveErrors}
            resolvedArtifacts={proposals.resolvedArtifacts}
            turnProposals={(turnId) =>
              proposals.forTurn(chat.sideConversationId, turnId)
            }
            onResolveArtifactProposal={(proposalId, decision) =>
              void proposals
                .resolveArtifactProposal(proposalId, decision)
                .then(hub.refresh)
            }
            onResolveKnowledgeProposal={(proposalId, decision) =>
              void proposals
                .resolveKnowledgeProposal(proposalId, decision)
                .then(hub.refresh)
            }
            onResolveMemoryProposal={(proposalId, decision) =>
              void proposals
                .resolveMemoryProposal(proposalId, decision)
                .then(hub.refresh)
            }
            onResolveTaskProposal={(proposalId, decision) =>
              void proposals
                .resolveTaskProposal(proposalId, decision)
                .then(hub.refresh)
            }
          />
        </section>
      ) : null}
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
          onClick={() => {
            setAssistantOpen(false);
            setWorkspaceCollapsed(false);
          }}
          type="button"
        >
          工作区
        </button>
      ) : null}
      {workspaceVisible ? (
        <button
          className="workspace-fab"
          onClick={() => {
            const opening = !workspaceDrawerOpen;
            setWorkspaceDrawerOpen(opening);
            if (opening) setAssistantOpen(false);
          }}
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
      {chat.sideConversationId ? (
        <button
          aria-label="收起临时会话"
          className="side-scrim"
          onClick={chat.closeSideConversation}
          type="button"
        />
      ) : null}
      <CornerHub
        onOpenAssistant={openAssistantPanel}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenWorkspace={openWorkspacePanel}
        pendingTotal={hub.total}
        permissionMode={permissionMode}
        workspaceAvailable={workspaceVisible}
      />
      {assistantOpen ? (
        <AssistantPanel
          onClose={() => setAssistantOpen(false)}
          onOpenConversation={(conversationId) => {
            void chat.openConversation(conversationId);
          }}
          onTabChange={setAssistantTab}
          pendingProposals={hub.proposals}
          tab={assistantTab}
          unreadCount={hub.unread}
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
