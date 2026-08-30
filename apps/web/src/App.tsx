import { useCallback, useEffect, useMemo, useState } from "react";
import { WorkspacePanel } from "./features/artifacts/WorkspacePanel";
import { chatApi } from "./features/chat/api";
import type { TaskNotification } from "./features/chat/apiTypes";
import { AssistantPanel } from "./features/panel/AssistantPanel";
import type { AssistantPanelTab } from "./features/panel/AssistantPanel";
import { useAssistantHub } from "./features/hub/useAssistantHub";
import { SettingsOverlay } from "./features/settings/SettingsOverlay";
import { WorkspaceSettingsModal } from "./features/workspace/WorkspaceSettingsModal";
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

  const railCollapsed = railPreferredCollapsed;

  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [assistantTab, setAssistantTab] = useState<AssistantPanelTab>("notifications");
  const [toasts, setToasts] = useState<TaskNotification[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [workspaceSettingsOpen, setWorkspaceSettingsOpen] = useState(false);
  const activeWorkspace = useMemo(
    () =>
      chat.workspaces.find(
        (item) => item.id === chat.activeSnapshot?.conversation.workspaceId,
      ) ?? null,
    [chat.activeSnapshot?.conversation.workspaceId, chat.workspaces],
  );

  useEffect(() => {
    setWorkspaceCollapsed(false);
    setWorkspaceDrawerOpen(false);
    setWorkspaceSettingsOpen(false);
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
    void chat.refreshCapabilities();
    setSettingsOpen(false);
    setWorkspaceSettingsOpen(false);
    setRailOpen(false);
    setAssistantOpen(true);
    setWorkspaceCollapsed(true);
    setWorkspaceDrawerOpen(false);
  };

  const openWorkspacePanel = async () => {
    if (chat.sideConversationId) {
      const closed = await chat.closeSideConversation();
      if (!closed) return;
    }
    setSettingsOpen(false);
    setWorkspaceSettingsOpen(false);
    setRailOpen(false);
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
        collapsed={railCollapsed}
        conversations={chat.conversations}
        open={railOpen}
        pendingTotal={hub.total}
        search={chat.search}
        statusFilter={chat.statusFilter}
        workspaceId={chat.workspaceId}
        workspaces={chat.workspaces}
        onCreateWorkspace={(name) => chat.createWorkspace(name)}
        onSelectWorkspace={chat.selectWorkspace}
        onClose={() => {
          if (window.innerWidth <= 760) {
            setRailOpen(false);
            return;
          }
          setRailPreferredCollapsed((current) => !current);
        }}
        onNewConversation={() => {
          void chat.newConversation();
          setRailOpen(false);
        }}
        onSearchChange={chat.setSearch}
        onSelectConversation={(conversationId) => {
          void chat.openConversation(conversationId);
          setRailOpen(false);
        }}
        onOpenAssistant={(tab) => {
          setAssistantTab(tab);
          openAssistantPanel();
          setRailOpen(false);
        }}
        onOpenSettings={() => {
          setAssistantOpen(false);
          setWorkspaceDrawerOpen(false);
          setWorkspaceSettingsOpen(false);
          setSettingsOpen(true);
          setRailOpen(false);
        }}
        onStatusFilterChange={chat.setStatusFilter}
        onChangeConversationStatus={(conversationId, status) =>
          void chat.changeConversationStatus(status, conversationId)
        }
        onDeleteConversation={(conversationId) =>
          void chat.deleteConversation(conversationId)
        }
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
        workspaces={chat.workspaces}
        liveTurns={chat.liveTurns}
        loading={chat.loading}
        pendingAction={chat.pendingAction}
        providers={chat.providers}
        runtimeConnectionPhase={chat.activeRuntimeConnection?.phase}
        runtimeSnapshot={chat.activeRuntimeSnapshot}
        branchLanes={
          chat.activeConversationId
            ? (chat.laneTrees[chat.activeConversationId] ?? [])
            : []
        }
        currentLaneId={
          chat.activeConversationId
            ? (chat.viewLaneIds[chat.activeConversationId] ??
              chat.mainLaneIds[chat.activeConversationId] ??
              null)
            : null
        }
        onArchive={() => void chat.changeConversationStatus("archived")}
        onArchiveLane={(laneId, includeArchived) => {
          if (!chat.activeConversationId) return;
          void chat.setLaneArchived(
            chat.activeConversationId,
            laneId,
            true,
            includeArchived,
          );
        }}
        onCancel={() => void chat.cancel()}
        onCancelRunningRun={(runId) => void chat.cancelRuntimeRun(runId)}
        onCreateBranch={(forkTurnId) => {
          if (!chat.activeConversationId) return;
          const sourceLaneId =
            chat.viewLaneIds[chat.activeConversationId] ??
            chat.mainLaneIds[chat.activeConversationId];
          if (!sourceLaneId) return;
          void chat.forkLane(
            chat.activeConversationId,
            sourceLaneId,
            forkTurnId,
          );
        }}
        onCreateTemporaryConversation={() =>
          void chat.createTemporaryConversation()
        }
        onDelete={() => void chat.deleteConversation()}
        onDismissError={chat.dismissPrimaryError}
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
        onResolveRuntimeRecovery={(runId, action) =>
          void chat.resolveRuntimeRecovery(runId, action)
        }
        onRemoveFile={(fileId) => void chat.removeFile(fileId)}
        onRename={(title) => void chat.renameConversation(title)}
        onRestore={() => void chat.changeConversationStatus("active")}
        onRetry={(turnId) => void chat.retry(turnId)}
        onSelectVariant={(turnId, variantId) =>
          void chat.selectVariant(turnId, variantId)
        }
        onSend={() => void chat.send()}
        onModelChange={(providerProfileId, modelOverride) =>
          void chat.changeConversationModel(providerProfileId, modelOverride)
        }
        onUploadFile={(file) => void chat.uploadFile(file)}
        onOpenAssistantTab={(tab) => {
          setAssistantTab(tab);
          openAssistantPanel();
        }}
        onOpenConversation={(conversationId) =>
          void chat.openConversation(conversationId)
        }
        onOpenRunningLane={(laneId) => {
          if (!chat.activeConversationId) return;
          void chat.switchLane(chat.activeConversationId, laneId);
        }}
        onOpenWorkspace={openWorkspacePanel}
        onOpenWorkspaceSettings={
          activeWorkspace
            ? () => {
                setSettingsOpen(false);
                setAssistantOpen(false);
                setWorkspaceDrawerOpen(false);
                setWorkspaceSettingsOpen(true);
              }
            : undefined
        }
        onOpenLaneInSide={(laneId) => {
          if (!chat.activeConversationId) return;
          void chat.openLaneInSide(chat.activeConversationId, laneId);
        }}
        onPromoteLane={(laneId) => {
          if (!chat.activeConversationId) return;
          void chat.promoteLane(chat.activeConversationId, laneId);
        }}
        onRenameLane={(laneId, displayName) => {
          if (!chat.activeConversationId) return;
          void chat.renameLane(
            chat.activeConversationId,
            laneId,
            displayName,
          );
        }}
        onRestoreLane={(laneId) => {
          if (!chat.activeConversationId) return;
          void chat.setLaneArchived(
            chat.activeConversationId,
            laneId,
            false,
            true,
          );
        }}
        onShowArchivedLanes={(visible) =>
          chat.activeConversationId
            ? chat.setArchivedLanesVisible(chat.activeConversationId, visible)
            : Promise.resolve()
        }
        onSwitchLane={(laneId) => {
          if (!chat.activeConversationId) return;
          void chat.switchLane(chat.activeConversationId, laneId);
        }}
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
        onResolveKnowledgeProposal={(proposalId, decision, workspaceId) =>
          void proposals
            .resolveKnowledgeProposal(proposalId, decision, workspaceId)
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
        <section
          aria-label={
            chat.sideMode === "branch_lane" ? "分支对照" : "临时对话"
          }
          className="side-chat-panel"
        >
          <ChatWorkSurface
            conversation={chat.sideSnapshot}
            draft={chat.sideDraft}
            error={chat.sideError}
            health={chat.health}
            isGenerating={chat.sideIsGenerating}
            liveTurns={chat.liveTurns}
            workspaces={chat.workspaces}
            loading={chat.sideLoading}
            pendingAction={chat.sidePendingAction}
            providers={chat.providers}
            branchLanes={
              chat.sideConversationId
                ? (chat.laneTrees[chat.sideConversationId] ?? [])
                : []
            }
            currentLaneId={chat.sideRuntimeSnapshot?.activeLaneId ?? null}
            runtimeConnectionPhase={chat.sideRuntimeConnection?.phase}
            runtimeSnapshot={chat.sideRuntimeSnapshot}
            sideMode={chat.sideMode}
            variant="side"
            onArchive={() => {}}
            onCancel={() =>
              void chat.cancel(chat.sideConversationId ?? undefined, "side")
            }
            onCancelRunningRun={(runId) =>
              void chat.cancelRuntimeRun(runId, "side")
            }
            onCloseSide={chat.closeSideConversation}
            onDelete={() => {}}
            onDismissError={chat.dismissSideError}
            onDraftChange={chat.setSideDraft}
            onMenu={() => {}}
            onPromote={() =>
              void chat.promoteConversation(
                chat.sideConversationId ?? undefined,
                "side",
              )
            }
            onRegenerate={(turnId) =>
              void chat.regenerate(
                turnId,
                chat.sideConversationId ?? undefined,
                "side",
              )
            }
            onResolveApproval={(turnId, approvalId, decision) =>
              void chat.resolveApproval(turnId, approvalId, decision, "side")
            }
            onResolveRuntimeRecovery={(runId, action) =>
              void chat.resolveRuntimeRecovery(runId, action, "side")
            }
            onOpenAssistantTab={(tab) => {
              setAssistantTab(tab);
              openAssistantPanel();
            }}
            onOpenWorkspace={() => {
              if (chat.sideMode !== "temporary_conversation") {
                void openWorkspacePanel();
                return;
              }
              void chat.focusTemporaryConversation().then((focused) => {
                if (!focused) return;
                setSettingsOpen(false);
                setWorkspaceSettingsOpen(false);
                setRailOpen(false);
                setAssistantOpen(false);
                setWorkspaceCollapsed(false);
                setWorkspaceDrawerOpen(true);
              });
            }}
            onOpenRunningLane={(laneId) => {
              if (
                !chat.activeConversationId ||
                chat.activeConversationId !== chat.sideConversationId
              ) {
                return;
              }
              void chat.switchLane(chat.activeConversationId, laneId);
            }}
            onRemoveFile={() => {}}
            onRename={() => {}}
            onRestore={() => {}}
            onRetry={(turnId) =>
              void chat.retry(
                turnId,
                chat.sideConversationId ?? undefined,
                "side",
              )
            }
            onSelectVariant={(turnId, variantId) =>
              void chat.selectVariant(
                turnId,
                variantId,
                chat.sideConversationId ?? undefined,
                "side",
              )
            }
            onSend={() => void chat.sendSide()}
            onModelChange={(providerProfileId, modelOverride) =>
              void chat.changeConversationModel(
                providerProfileId,
                modelOverride,
                chat.sideConversationId ?? undefined,
                "side",
              )
            }
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
            onResolveKnowledgeProposal={(proposalId, decision, workspaceId) =>
              void proposals
                .resolveKnowledgeProposal(proposalId, decision, workspaceId)
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
          workspaceRootPath={
            chat.workspaces.find((item) => item.id === chat.workspaceId)?.rootPath ??
            null
          }
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
      {assistantOpen ? (
        <AssistantPanel
          capabilities={chat.capabilities}
          conversationId={chat.activeConversationId}
          onCapabilitiesChanged={() => void chat.refreshCapabilities()}
          onClose={() => setAssistantOpen(false)}
          onOpenConversation={(conversationId) => {
            void chat.openConversation(conversationId);
          }}
          onTabChange={setAssistantTab}
          pendingProposals={hub.proposals}
          onProvidersChanged={() => void chat.refreshProviders()}
          runtimeConnection={chat.activeRuntimeConnection}
          runtimeEvents={chat.activeRuntimeEvents}
          runtimeLanes={
            chat.activeConversationId
              ? (chat.laneTrees[chat.activeConversationId] ?? [])
              : []
          }
          runtimeSnapshot={chat.activeRuntimeSnapshot}
          runtimeStatus={chat.activeRuntimeStatus}
          tab={assistantTab}
          unreadCount={hub.unread}
          workspaceId={chat.workspaceId}
          workspaces={chat.workspaces}
        />
      ) : null}
      <div
        aria-live="polite"
        aria-relevant="additions"
        className="toast-stack"
        role="status"
      >
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
          onModeChanged={() => undefined}
        />
      ) : null}
      {workspaceSettingsOpen && activeWorkspace ? (
        <WorkspaceSettingsModal
          onClose={() => setWorkspaceSettingsOpen(false)}
          onWorkspaceUpdated={() => {
            void chat.refreshWorkspaces();
          }}
          workspace={activeWorkspace}
        />
      ) : null}
    </div>
  );
}
