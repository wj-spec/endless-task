import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
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
import { ResizableChatSplit } from "./features/chat/ResizableChatSplit";
import { SessionRail } from "./features/chat/SessionRail";
import { useChatApplication } from "./features/chat/useChatApplication";
import { useProposals } from "./features/proposals/useProposals";

const TERMINAL_TURN_STATUSES = new Set(["completed", "failed", "cancelled"]);

type AuxiliarySurface =
  | { type: "none" }
  | { type: "rail" }
  | { type: "workspace" }
  | { type: "assistant"; tab: AssistantPanelTab }
  | { type: "settings" }
  | { type: "workspace-settings" }
  | { type: "workspace-create" };

type AuxiliarySurfaceAction =
  | { type: "close" }
  | { type: "close-workspace-surfaces" }
  | { type: "open-rail" }
  | { type: "open-workspace" }
  | { type: "open-assistant"; tab: AssistantPanelTab }
  | { type: "open-settings" }
  | { type: "open-workspace-settings" }
  | { type: "open-workspace-create" };

const reduceAuxiliarySurface = (
  _current: AuxiliarySurface,
  action: AuxiliarySurfaceAction,
): AuxiliarySurface => {
  switch (action.type) {
    case "open-rail":
      return { type: "rail" };
    case "open-workspace":
      return { type: "workspace" };
    case "open-assistant":
      return { type: "assistant", tab: action.tab };
    case "open-settings":
      return { type: "settings" };
    case "open-workspace-settings":
      return { type: "workspace-settings" };
    case "open-workspace-create":
      return { type: "workspace-create" };
    case "close-workspace-surfaces":
      return _current.type === "workspace" ||
        _current.type === "workspace-settings"
        ? { type: "none" }
        : _current;
    case "close":
      return { type: "none" };
  }
};

export function App() {
  const chat = useChatApplication();
  const proposals = useProposals(
    chat.activeConversationId,
    chat.sideConversationId,
  );
  const [activeSurface, dispatchSurface] = useReducer(reduceAuxiliarySurface, {
    type: "none",
  });
  const railOpen = activeSurface.type === "rail";
  const workspaceDrawerOpen = activeSurface.type === "workspace";
  const assistantOpen = activeSurface.type === "assistant";
  const settingsOpen = activeSurface.type === "settings";
  const workspaceSettingsOpen = activeSurface.type === "workspace-settings";
  const workspaceCreateOpen = activeSurface.type === "workspace-create";
  const assistantTab =
    activeSurface.type === "assistant" ? activeSurface.tab : "notifications";
  const [railPreferredCollapsed, setRailPreferredCollapsed] = useState(
    () => window.innerWidth < 1180,
  );
  // 工作区设置的目标：默认是当前会话的工作区；从侧栏「绑定目录」可指定具体工作区。
  const [workspaceSettingsTargetId, setWorkspaceSettingsTargetId] = useState<
    string | null
  >(null);

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
    dispatchSurface({ type: "close" });
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

  const [toasts, setToasts] = useState<TaskNotification[]>([]);
  const activeWorkspace = useMemo(
    () =>
      chat.workspaces.find(
        (item) => item.id === chat.activeSnapshot?.conversation.workspaceId,
      ) ?? null,
    [chat.activeSnapshot?.conversation.workspaceId, chat.workspaces],
  );

  const workspaceSettingsTarget =
    chat.workspaces.find((item) => item.id === workspaceSettingsTargetId) ??
    activeWorkspace;

  useEffect(() => {
    setWorkspaceCollapsed(false);
    dispatchSurface({ type: "close-workspace-surfaces" });
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

  const openAssistantPanel = (tab: AssistantPanelTab = "notifications") => {
    void chat.refreshCapabilities();
    dispatchSurface({ type: "open-assistant", tab });
    setWorkspaceCollapsed(true);
  };

  const openWorkspacePanel = async () => {
    if (chat.sideConversationId) {
      const closed = await chat.closeSideConversation();
      if (!closed) return;
    }
    dispatchSurface({ type: "open-workspace" });
    setWorkspaceCollapsed(false);
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
        open={railOpen}
        pendingTotal={hub.total}
        search={chat.search}
        statusFilter={chat.statusFilter}
        workspaceId={chat.workspaceId}
        workspaces={chat.workspaces}
        workspaceCanCreate={chat.workspaceCanCreate}
        currentWorkspace={chat.currentWorkspace}
        onCreateWorkspace={() => dispatchSurface({ type: "open-workspace-create" })}
        onRequestBindWorkspace={(workspaceId) => {
          setWorkspaceSettingsTargetId(workspaceId);
          dispatchSurface({ type: "open-workspace-settings" });
        }}
        onDeleteWorkspace={(workspaceId) => chat.deleteWorkspace(workspaceId)}
        onClose={() => {
          if (window.innerWidth <= 760) {
            dispatchSurface({ type: "close" });
            return;
          }
          setRailPreferredCollapsed((current) => !current);
        }}
        onNewConversation={() => {
          void chat.newConversation();
          dispatchSurface({ type: "close" });
        }}
        onSearchChange={chat.setSearch}
        onSelectConversation={(conversationId) => {
          void chat.openConversation(conversationId);
          dispatchSurface({ type: "close" });
        }}
        onOpenAssistant={openAssistantPanel}
        onOpenSettings={() => dispatchSurface({ type: "open-settings" })}
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
      <ResizableChatSplit sideOpen={Boolean(chat.sideConversationId)}>
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
        runtimeEvents={chat.activeRuntimeEvents}
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
          void chat.forkLane(chat.activeConversationId, sourceLaneId, forkTurnId);
        }}
        onCreateTemporaryConversation={() =>
          void chat.createTemporaryConversation()
        }
        onDelete={() => void chat.deleteConversation()}
        onDismissError={chat.dismissPrimaryError}
        onDraftChange={chat.setDraft}
        onMenu={() => {
          if (window.innerWidth <= 760) dispatchSurface({ type: "open-rail" });
          else setRailPreferredCollapsed(false);
        }}
        onPromote={() => void chat.promoteConversation()}
        onRegenerate={(turnId) => void chat.regenerate(turnId)}
        onEditResendMessage={(turnId, content) => void chat.resend(turnId, content)}
        editedUserMessages={chat.editedUserMessages}
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
        onOpenAssistantTab={openAssistantPanel}
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
                setWorkspaceSettingsTargetId(activeWorkspace.id);
                dispatchSurface({ type: "open-workspace-settings" });
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
            onOpenAssistantTab={openAssistantPanel}
            onOpenWorkspace={() => {
              if (chat.sideMode !== "temporary_conversation") {
                void openWorkspacePanel();
                return;
              }
              void chat.focusTemporaryConversation().then((focused) => {
                if (!focused) return;
                setWorkspaceCollapsed(false);
                dispatchSurface({ type: "open-workspace" });
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
      </ResizableChatSplit>
      {workspaceVisible && (!workspaceCollapsed || workspaceDrawerOpen) ? (
        <WorkspacePanel
          conversationId={workspace.workspace!.conversationId}
          drawerOpen={workspaceDrawerOpen}
          latestTurnId={latestTurn?.turn.id ?? null}
          onCollapse={() => {
            setWorkspaceCollapsed(true);
            dispatchSurface({ type: "close-workspace-surfaces" });
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
            dispatchSurface({ type: "close" });
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
          onClick={() =>
            dispatchSurface({
              type: workspaceDrawerOpen ? "close" : "open-workspace",
            })
          }
          type="button"
        >
          工作区
        </button>
      ) : null}
      {workspaceVisible && workspaceDrawerOpen ? (
        <button
          aria-label="关闭工作区"
          className="workspace-scrim"
          onClick={() => dispatchSurface({ type: "close" })}
          type="button"
        />
      ) : null}
      {railOpen ? (
        <button
          aria-label="关闭会话列表"
          className="rail-scrim"
          onClick={() => dispatchSurface({ type: "close" })}
          type="button"
        />
      ) : null}
      {assistantOpen ? (
        <AssistantPanel
          capabilities={chat.capabilities}
          conversationId={chat.activeConversationId}
          onCapabilitiesChanged={() => void chat.refreshCapabilities()}
          onClose={() => dispatchSurface({ type: "close" })}
          onOpenConversation={(conversationId) => {
            void chat.openConversation(conversationId);
          }}
          onTabChange={(tab) =>
            dispatchSurface({ type: "open-assistant", tab })
          }
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
          onClose={() => dispatchSurface({ type: "close" })}
          onModeChanged={() => undefined}
        />
      ) : null}
      {workspaceSettingsOpen && workspaceSettingsTarget ? (
        <WorkspaceSettingsModal
          onClose={() => {
            setWorkspaceSettingsTargetId(null);
            dispatchSurface({ type: "close" });
          }}
          onCreateWorkspace={(name, rootPath) => chat.createWorkspace(name, rootPath)}
          onDeleteWorkspace={async (workspaceId) => {
            await chat.deleteWorkspace(workspaceId);
            dispatchSurface({ type: "close" });
          }}
          onWorkspaceUpdated={() => {
            void chat.refreshWorkspaces();
          }}
          workspace={workspaceSettingsTarget}
        />
      ) : null}
      {workspaceCreateOpen ? (
        <WorkspaceSettingsModal
          createMode
          onClose={() => dispatchSurface({ type: "close" })}
          onCreateWorkspace={(name, rootPath) => chat.createWorkspace(name, rootPath)}
          onDeleteWorkspace={async (workspaceId) => {
            await chat.deleteWorkspace(workspaceId);
            dispatchSurface({ type: "close" });
          }}
          onWorkspaceUpdated={() => {
            void chat.refreshWorkspaces();
          }}
          workspace={null}
        />
      ) : null}
    </div>
  );
}
