import { useEffect, useMemo, useState } from "react";
import { WorkspacePanel } from "./features/artifacts/WorkspacePanel";
import { MemoryManagement } from "./features/memory/MemoryManagement";
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

  useEffect(() => {
    setWorkspaceCollapsed(false);
    setWorkspaceDrawerOpen(false);
  }, [chat.activeConversationId]);

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
    </div>
  );
}
