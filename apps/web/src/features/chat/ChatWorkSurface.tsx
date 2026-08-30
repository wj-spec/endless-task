import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type {
  ArtifactRecordSummary,
  ConversationSnapshot,
  HealthSnapshot,
  KnowledgeCitation,
  LiveTurn,
  ProviderProfile,
  ResponseVariantSnapshot,
  RuntimeV2Lane,
  RuntimeV2Snapshot,
  TurnStatus,
  Workspace,
} from "./apiTypes";
import { chatApi } from "./api";
import type { RuntimeConnectionPhase } from "./runtimeController";
import { CitationCard } from "./CitationCard";
import { ArtifactProposalCard } from "../proposals/ArtifactProposalCard";
import { KnowledgeProposalCard } from "../proposals/KnowledgeProposalCard";
import { MemoryProposalCard } from "../proposals/MemoryProposalCard";
import { TaskProposalCard } from "../proposals/TaskProposalCard";
import type { TurnProposals } from "../proposals/useProposals";
import { CollapsibleMessage } from "./CollapsibleMessage";
import { SearchBar } from "./SearchBar";
import { useConversationSearch } from "./useConversationSearch";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { RowMenu } from "../ui/RowMenu";
import { SidebarToggleIcon } from "../ui/SidebarToggleIcon";
import { StatusBadge } from "../ui/StatusBadge";
import { BranchNavigator } from "./BranchNavigator";

type ChatWorkSurfaceProps = {
  conversation: ConversationSnapshot | null;
  draft: string;
  error: string | null;
  health: HealthSnapshot | null;
  isGenerating: boolean;
  liveTurns: Record<string, LiveTurn>;
  loading: boolean;
  pendingAction: string | null;
  providers: ProviderProfile[];
  runtimeConnectionPhase?: RuntimeConnectionPhase;
  runtimeSnapshot?: RuntimeV2Snapshot | null;
  onArchive: () => void;
  onCancel: () => void;
  onCancelRunningRun?: (runId: string) => void;
  onCreateBranch?: (forkTurnId?: string) => void;
  onCreateTemporaryConversation?: (forkTurnId?: string) => void;
  onDelete: () => void;
  onDismissError: () => void;
  onDraftChange: (value: string) => void;
  onMenu: () => void;
  onPromote: () => void;
  onRegenerate: (turnId: string) => void;
  onResolveApproval: (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny",
  ) => void;
  onResolveRuntimeRecovery?: (
    runId: string,
    action: "retry" | "mark_failed",
  ) => void;
  onRemoveFile: (fileId: string) => void;
  onRename: (title: string) => void;
  onRestore: () => void;
  onRetry: (turnId: string) => void;
  onSelectVariant: (turnId: string, variantId: string) => void;
  onSend: () => void;
  onModelChange: (
    providerProfileId: string | null,
    modelOverride: string | null,
  ) => void;
  onUploadFile: (file: File) => void;
  proposalBusyId: string | null;
  proposalErrors: Record<string, string>;
  resolvedArtifacts: Record<string, ArtifactRecordSummary>;
  turnProposals: (turnId: string) => TurnProposals;
  onResolveArtifactProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onResolveKnowledgeProposal: (
    proposalId: string,
    decision: "accept" | "reject",
    workspaceId?: string | null,
  ) => void;
  onResolveMemoryProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onResolveTaskProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onCloseSide?: () => void;
  onOpenAssistantTab?: (
    tab: "memory" | "knowledge" | "providers" | "scheduled",
  ) => void;
  workspaces?: Workspace[];
  onOpenConversation?: (conversationId: string) => void;
  onOpenRunningLane?: (laneId: string) => void;
  onOpenWorkspace?: () => void;
  onOpenWorkspaceSettings?: () => void;
  branchLanes?: RuntimeV2Lane[];
  currentLaneId?: string | null;
  onArchiveLane?: (laneId: string, includeArchived: boolean) => void;
  onOpenLaneInSide?: (laneId: string) => void;
  onPromoteLane?: (laneId: string) => void;
  onRenameLane?: (laneId: string, displayName: string | null) => void;
  onRestoreLane?: (laneId: string) => void;
  onShowArchivedLanes?: (visible: boolean) => void | Promise<void>;
  onSwitchLane?: (laneId: string) => void;
  sideMode?: "temporary_conversation" | "branch_lane" | null;
  variant?: "main" | "side";
};

type TurnStatusPresentation = {
  label: string;
  tone: "neutral" | "active" | "warning" | "danger";
  pulse: boolean;
};

const turnStatusPresentation = (
  status: TurnStatus,
  waitingApproval: boolean,
): TurnStatusPresentation | null => {
  if (waitingApproval) {
    return { label: "等待确认", tone: "warning", pulse: false };
  }
  if (status === "created") {
    return { label: "准备回答", tone: "active", pulse: true };
  }
  if (status === "running") {
    return { label: "正在回答", tone: "active", pulse: true };
  }
  if (status === "failed") {
    return { label: "回答失败", tone: "danger", pulse: false };
  }
  if (status === "cancelled") {
    return { label: "已停止", tone: "neutral", pulse: false };
  }
  return null;
};

function findActiveVariant(
  variants: ResponseVariantSnapshot[],
  activeId: string | null,
) {
  return variants.find((item) => item.variant.id === activeId) ?? variants.at(-1);
}

export function ChatWorkSurface({
  conversation,
  draft,
  error,
  health,
  isGenerating,
  liveTurns,
  loading,
  pendingAction,
  providers,
  runtimeConnectionPhase = "idle",
  runtimeSnapshot = null,
  onArchive,
  onCancel,
  onCancelRunningRun,
  onCreateBranch,
  onCreateTemporaryConversation,
  onDelete,
  onDismissError,
  onDraftChange,
  onMenu,
  onPromote,
  onRegenerate,
  onResolveApproval,
  onResolveRuntimeRecovery,
  onRemoveFile,
  onRename,
  onRestore,
  onRetry,
  onSelectVariant,
  onSend,
  onModelChange,
  onUploadFile,
  proposalBusyId,
  proposalErrors,
  resolvedArtifacts,
  turnProposals,
  onResolveArtifactProposal,
  onResolveKnowledgeProposal,
  onResolveMemoryProposal,
  onResolveTaskProposal,
  onCloseSide,
  onOpenAssistantTab,
  onOpenConversation,
  onOpenRunningLane,
  onOpenWorkspace,
  onOpenWorkspaceSettings,
  workspaces,
  branchLanes = [],
  currentLaneId = null,
  onArchiveLane,
  onOpenLaneInSide,
  onPromoteLane,
  onRenameLane,
  onRestoreLane,
  onShowArchivedLanes,
  onSwitchLane,
  sideMode = null,
  variant = "main",
}: ChatWorkSurfaceProps) {
  const streamRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const search = useConversationSearch(conversation, liveTurns, streamRef);
  const [citationsByTurn, setCitationsByTurn] = useState<
    Record<string, KnowledgeCitation[]>
  >({});
  const [openCitation, setOpenCitation] = useState<{
    turnId: string;
    label: string;
  } | null>(null);
  const [modelDraft, setModelDraft] = useState("");

  useEffect(() => {
    setModelDraft(conversation?.conversation.modelOverride ?? "");
  }, [conversation?.conversation.id, conversation?.conversation.modelOverride]);

  const handleCitationClick = async (turnId: string, label: string) => {
    if (
      openCitation &&
      openCitation.turnId === turnId &&
      openCitation.label === label
    ) {
      setOpenCitation(null);
      return;
    }
    setOpenCitation({ turnId, label });
    let items = citationsByTurn[turnId];
    if (!items) {
      try {
        items = await chatApi.getTurnCitations(turnId);
      } catch {
        items = [];
      }
      setCitationsByTurn((current) => ({ ...current, [turnId]: items ?? [] }));
    }
    const citation = items.find((item) => item.label === label);
    if (citation) {
      void chatApi
        .recordCitationClick({
          label: citation.label,
          scope: citation.scope,
          refId: citation.refId,
          turnId,
          conversationId: citation.conversationId,
        })
        .catch(() => undefined);
    }
  };

  const jumpCitation = (citation: KnowledgeCitation) => {
    if (citation.scope === "conversation") {
      if (citation.conversationId && onOpenConversation) {
        onOpenConversation(citation.conversationId);
      }
      return;
    }
    if (citation.scope === "artifact") {
      onOpenWorkspace?.();
      return;
    }
    if (citation.scope === "memory") onOpenAssistantTab?.("memory");
    if (citation.scope === "source") onOpenAssistantTab?.("knowledge");
  };

  const conversationId = conversation?.conversation.id;
  const currentLane = branchLanes.find((lane) => lane.id === currentLaneId) ?? null;
  const mainLane = branchLanes.find((lane) => lane.isMain) ?? null;
  const viewingBranch = variant === "main" && currentLane !== null && !currentLane.isMain;
  const currentLaneLabel =
    currentLane?.displayName ?? currentLane?.title ?? currentLane?.summary ?? "未命名分支";
  const currentLaneHasActiveRun = Boolean(
    runtimeSnapshot?.runningRunId &&
      runtimeSnapshot.runningLaneId &&
      runtimeSnapshot.runningLaneId === currentLaneId,
  );
  const requestSideClose = useCallback(() => {
    if (!onCloseSide) return;
    if (sideMode === "temporary_conversation" || currentLaneHasActiveRun) {
      setConfirmingClose(true);
      return;
    }
    onCloseSide();
  }, [currentLaneHasActiveRun, onCloseSide, sideMode]);

  useEffect(() => {
    if (variant !== "side") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      event.preventDefault();
      requestSideClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [requestSideClose, variant]);
  const firstOwnTurnIndex = conversationId
    ? (conversation?.turns ?? []).findIndex(
        (item) => item.turn.conversationId === conversationId,
      )
    : -1;
  const hasInheritedTurns =
    firstOwnTurnIndex !== 0 &&
    (conversation?.turns.some(
      (item) => item.turn.conversationId !== conversationId,
    ) ?? false);
  const latestTurnId = conversation?.turns.at(-1)?.turn.id;
  const latestLiveContent = latestTurnId ? liveTurns[latestTurnId]?.content : undefined;

  useEffect(() => {
    setRenaming(false);
    setTitleDraft(conversation?.conversation.title ?? "");
    setOpenCitation(null);
  }, [conversationId, conversation?.conversation.title]);

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: "smooth" });
  }, [conversation?.turns.length, latestLiveContent]);

  useEffect(() => {
    if (variant === "side" && !loading && conversation) {
      composerRef.current?.focus();
    }
  }, [variant, loading, conversationId]);

  const submitTitle = () => {
    const title = titleDraft.trim();
    if (title && title !== conversation?.conversation.title) onRename(title);
    setRenaming(false);
  };

  const archived = conversation?.conversation.status === "archived";
  const defaultProvider = providers.find((item) => item.isDefault) ?? providers[0];
  const selectedProvider = providers.find(
    (item) => item.id === conversation?.conversation.providerProfileId,
  );
  const effectiveProvider = selectedProvider ?? defaultProvider;
  const providerUnavailable =
    (health !== null && !health.providerConfigured) ||
    effectiveProvider?.configured === false;
  const runningLane = runtimeSnapshot?.runningLaneId
    ? branchLanes.find((lane) => lane.id === runtimeSnapshot.runningLaneId) ?? null
    : null;
  const otherLaneRunning = Boolean(
    runtimeSnapshot?.runningLaneId &&
      currentLaneId &&
      runtimeSnapshot.runningLaneId !== currentLaneId,
  );
  const runningLaneLabel =
    runningLane?.displayName ?? runningLane?.title ?? runningLane?.summary ?? "另一分支";
  const composerDisabled =
    !conversation || archived || providerUnavailable || otherLaneRunning;
  const selectableProviders = providers.filter(
    (item) => item.enabled || item.id === selectedProvider?.id,
  );

  const changeProvider = (value: string) => {
    if (value === "__manage__") {
      onOpenAssistantTab?.("providers");
      return;
    }
    onModelChange(value || null, modelDraft.trim() || null);
  };

  const commitModelDraft = () => {
    if (!conversation) return;
    const nextModel = modelDraft.trim();
    if (nextModel === (conversation.conversation.modelOverride ?? "")) return;
    onModelChange(conversation.conversation.providerProfileId, nextModel || null);
  };
  const attachmentDisabled =
    !conversation || archived || isGenerating || pendingAction !== null;

  const SurfaceRoot = variant === "side" ? "section" : "main";

  return (
    <SurfaceRoot
      className={variant === "side" ? "chat-surface is-side" : "chat-surface"}
    >
      <div className="surface-top">
        {variant === "side" ? (
          <header className="surface-header side-surface-header">
            <div className="conversation-heading">
              <h1>
                {sideMode === "branch_lane"
                  ? currentLaneLabel
                  : (conversation?.conversation.title ?? "临时对话")}
              </h1>
              {sideMode === "temporary_conversation" ? (
                <span className="temporary-close-hint">关闭即删除</span>
              ) : sideMode === "branch_lane" ? (
                <span className="temporary-close-hint">关闭仅收起，不删除分支</span>
              ) : null}
            </div>
            <div className="surface-header-side">
              <button
                aria-label={
                  sideMode === "branch_lane"
                    ? "收起分支对照"
                    : "关闭并删除临时对话"
                }
                className="icon-button side-close"
                onClick={requestSideClose}
                type="button"
              >
                <span aria-hidden="true">×</span>
              </button>
            </div>
          </header>
        ) : (
        <header className="surface-header">
          <button
            aria-label="展开侧栏"
            className="icon-button mobile-menu"
            onClick={onMenu}
            title="展开侧栏"
            type="button"
          >
            <SidebarToggleIcon expanded={false} />
          </button>
          <div className="conversation-heading">
            {renaming ? (
              <input
                aria-label="会话标题"
                autoFocus
                className="title-input"
                onBlur={submitTitle}
                onChange={(event) => setTitleDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") submitTitle();
                  if (event.key === "Escape") setRenaming(false);
                }}
                value={titleDraft}
              />
            ) : (
              <h1>{conversation?.conversation.title ?? "Endless"}</h1>
            )}
            {archived ? <span className="archived-chip">已归档</span> : null}
            {variant === "main" &&
            conversationId &&
            branchLanes.length > 0 &&
            onCreateBranch &&
            onArchiveLane &&
            onOpenLaneInSide &&
            onPromoteLane &&
            onRenameLane &&
            onRestoreLane &&
            onShowArchivedLanes &&
            onSwitchLane ? (
              <BranchNavigator
                conversationId={conversationId}
                currentLaneId={currentLaneId}
                disabled={isGenerating || pendingAction !== null}
                lanes={branchLanes}
                onArchive={onArchiveLane}
                onCreateBranch={() => onCreateBranch()}
                onOpenSide={onOpenLaneInSide}
                onPromote={onPromoteLane}
                onRename={onRenameLane}
                onRestore={onRestoreLane}
                onShowArchived={onShowArchivedLanes}
                onSwitch={onSwitchLane}
              />
            ) : null}
          </div>
          <div className="surface-header-side">
            {conversation ? (
              <div className="conversation-actions">
                <button
                  aria-label="搜索当前会话"
                  className="icon-button conversation-icon-button"
                  onClick={search.openSearch}
                  title="搜索当前会话"
                  type="button"
                >
                  <span aria-hidden="true">⌕</span>
                </button>
                <button
                  aria-label="开临时会话"
                  className="icon-button conversation-icon-button"
                  disabled={
                    isGenerating ||
                    pendingAction !== null ||
                    !onCreateTemporaryConversation ||
                    (conversation?.turns.length ?? 0) === 0
                  }
                  onClick={() => onCreateTemporaryConversation?.()}
                  title="基于当前对话开一个临时会话：深究或多方案并行，不污染原会话"
                  type="button"
                >
                  <span aria-hidden="true">⑂</span>
                </button>
                <RowMenu
                  trigger={<span aria-hidden="true">⋯</span>}
                  triggerAriaLabel="更多会话操作"
                  triggerClassName="icon-button conversation-icon-button"
                  items={[
                    { label: "重命名", onSelect: () => setRenaming(true) },
                    ...(onOpenWorkspaceSettings
                      ? [
                          {
                            label: "配置当前工作区",
                            onSelect: onOpenWorkspaceSettings,
                          },
                        ]
                      : []),
                    {
                      label: archived ? "恢复" : "归档",
                      disabled: isGenerating,
                      onSelect: archived ? onRestore : onArchive,
                    },
                    {
                      danger: true,
                      disabled: isGenerating,
                      label: "删除",
                      onSelect: () => setConfirmingDelete(true),
                    },
                  ]}
                />
              </div>
            ) : null}
          </div>
        </header>
        )}

        {conversation?.conversation.kind === "ephemeral" ? (
          <div className="branch-banner" role="note">
            <span className="branch-banner-text">
              临时会话
              {conversation.parentTitle
                ? ` · 源自《${conversation.parentTitle}》`
                : ""}
              {" "}· 不写入记忆，用完可丢弃
            </span>
            <button
              disabled={pendingAction !== null}
              onClick={onPromote}
              type="button"
            >
              升级为正式
            </button>
          </div>
        ) : null}

        {viewingBranch ? (
          <div className="branch-banner" role="note">
            <span className="branch-banner-text">
              正在查看分支「{currentLaneLabel}」；查看不会改变主线。
            </span>
            <div className="branch-banner-actions">
              {mainLane && onSwitchLane ? (
                <button
                  disabled={pendingAction !== null}
                  onClick={() => onSwitchLane(mainLane.id)}
                  type="button"
                >
                  返回主线
                </button>
              ) : null}
              {currentLane && onPromoteLane ? (
                <button
                  disabled={pendingAction !== null}
                  onClick={() => onPromoteLane(currentLane.id)}
                  type="button"
                >
                  设为主线
                </button>
              ) : null}
            </div>
          </div>
        ) : null}

        {otherLaneRunning ? (
          <div
            aria-atomic="true"
            aria-live="polite"
            className="branch-banner runtime-conflict-banner"
            role="status"
          >
            <span className="branch-banner-text">
              「{runningLaneLabel}」正在运行；同一会话暂不支持多分支并行。
            </span>
            <div className="branch-banner-actions">
              {runtimeSnapshot?.runningLaneId && onOpenRunningLane ? (
                <button
                  disabled={pendingAction !== null}
                  onClick={() => onOpenRunningLane(runtimeSnapshot.runningLaneId!)}
                  type="button"
                >
                  查看运行位置
                </button>
              ) : null}
              {runtimeSnapshot?.runningRunId && onCancelRunningRun ? (
                <button
                  disabled={pendingAction !== null}
                  onClick={() => onCancelRunningRun(runtimeSnapshot.runningRunId!)}
                  type="button"
                >
                  停止后继续
                </button>
              ) : null}
            </div>
          </div>
        ) : null}

        {search.open ? (
          <SearchBar
            current={search.current}
            hitCount={search.hitCount}
            onClose={search.close}
            onNext={search.next}
            onPrev={search.prev}
            onQueryChange={search.setQuery}
            query={search.query}
          />
        ) : null}

      </div>

      <div className="conversation-stream" ref={streamRef}>
        {error ? (
          <div className="inline-error" role="alert">
            <span>{error}</span>
            <button onClick={onDismissError} type="button">关闭</button>
          </div>
        ) : null}

        {runtimeConnectionPhase === "reconnecting" ? (
          <section
            aria-atomic="true"
            aria-live="polite"
            className="runtime-recovery-card is-connecting"
            role="status"
          >
            <strong>正在恢复连接</strong>
            <p>现有运行仍被保留，连接恢复前不会重复提交消息。</p>
          </section>
        ) : null}

        {onResolveRuntimeRecovery
          ? runtimeSnapshot?.interruptedRuns.map((report) => (
              <section
                className="runtime-recovery-card"
                key={report.runId}
                role="alert"
              >
                <strong>上次运行被中断</strong>
                <p>
                  {report.findings[0]?.message ??
                    "应用已恢复，但这次运行没有正常结束。"}
                </p>
                <div className="runtime-recovery-actions">
                  <button
                    disabled={pendingAction !== null}
                    onClick={() => onResolveRuntimeRecovery(report.runId, "retry")}
                    type="button"
                  >
                    安全重试
                  </button>
                  <button
                    disabled={pendingAction !== null}
                    onClick={() =>
                      onResolveRuntimeRecovery(report.runId, "mark_failed")
                    }
                    type="button"
                  >
                    结束本次运行
                  </button>
                </div>
              </section>
            ))
          : null}

        {loading && !conversation ? <LoadingState /> : null}
        {!loading && conversation?.turns.length === 0 ? (
          <EmptyConversation onSuggestion={onDraftChange} />
        ) : null}

        <div className="message-column">
          {conversation?.turns.map((turnSnapshot, turnIndex) => {
            const persistedVariant = findActiveVariant(
              turnSnapshot.responseVariants,
              turnSnapshot.turn.activeResponseVariantId,
            );
            if (!persistedVariant) return null;
            const live = liveTurns[turnSnapshot.turn.id];
            const useLive = live?.responseVariantId === persistedVariant.variant.id;
            const content = useLive
              ? live.content
              : persistedVariant.assistantMessage.content;
            const status = useLive ? live.status : turnSnapshot.turn.status;
            const pendingApproval = useLive ? live.pendingApproval : undefined;
            const statusPresentation = turnStatusPresentation(
              status,
              Boolean(pendingApproval),
            );
            const turnError = useLive ? live.error : undefined;
            const activities = useLive
              ? live.activities
              : (turnSnapshot.activities ?? []);
            const isLatest = turnIndex === conversation.turns.length - 1;
            const selectedIndex = turnSnapshot.responseVariants.findIndex(
              (item) => item.variant.id === persistedVariant.variant.id,
            );

            const dividerHere =
              hasInheritedTurns && turnIndex === firstOwnTurnIndex;
            return (
              <Fragment key={turnSnapshot.turn.id}>
              {dividerHere ? (
                <div className="lineage-divider" role="note">
                  以上继承自《{conversation.parentTitle ?? "主会话"}》，以下是本会话内容
                </div>
              ) : null}
              <section className="turn">
                {variant === "side" ||
                (!onCreateBranch && !onCreateTemporaryConversation) ? null : (
                  <RowMenu
                    className="turn-branch-menu"
                    disabled={isGenerating || pendingAction !== null}
                    items={[
                      ...(onCreateBranch
                        ? [
                            {
                              label: "从这里创建持久分支",
                              onSelect: () =>
                                onCreateBranch(turnSnapshot.turn.id),
                            },
                          ]
                        : []),
                      ...(onCreateTemporaryConversation
                        ? [
                            {
                              label: "从这里打开临时对话",
                              onSelect: () =>
                                onCreateTemporaryConversation(
                                  turnSnapshot.turn.id,
                                ),
                            },
                          ]
                        : []),
                    ]}
                    trigger={
                      <>
                        <span aria-hidden="true">⑂</span>
                        从这里开始
                      </>
                    }
                    triggerAriaLabel="从这条消息创建分支或临时对话"
                    triggerClassName="turn-fork"
                  />
                )}
                <article className="message-row user-row">
                  <div className="speaker-mark user-mark">你</div>
                  <div className="user-copy">{turnSnapshot.userMessage.content}</div>
                </article>

                <article className="message-row assistant-row">
                  <div className="speaker-mark assistant-mark" aria-label="Endless">
                    ∞
                  </div>
                  <div className="assistant-content">
                    {content ? (
                      <CollapsibleMessage
                        content={content}
                        forceExpand={search.forceExpandTurnIds.has(
                          turnSnapshot.turn.id,
                        )}
                        onCitationClick={(label) =>
                          void handleCitationClick(turnSnapshot.turn.id, label)
                        }
                        streaming={status === "created" || status === "running"}
                      />
                    ) : null}
                    {openCitation?.turnId === turnSnapshot.turn.id ? (
                      <CitationCard
                        citation={
                          (citationsByTurn[turnSnapshot.turn.id] ?? []).find(
                            (item) => item.label === openCitation.label,
                          ) ?? null
                        }
                        jumpDisabled={variant === "side"}
                        onClose={() => setOpenCitation(null)}
                        onJump={jumpCitation}
                      />
                    ) : null}
                    {activities.length ? (
                      <div className="activity-list" aria-label="操作状态">
                        {activities.map((activity) => (
                          <div
                            className={`activity-line is-${activity.status}`}
                            key={activity.id}
                          >
                            <span aria-hidden="true" />
                            {activity.message}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {statusPresentation ? (
                      <div
                        aria-atomic="true"
                        aria-live={status === "failed" ? "assertive" : "polite"}
                        className="turn-status"
                        role={status === "failed" ? "alert" : "status"}
                      >
                        <StatusBadge
                          label={statusPresentation.label}
                          pulse={statusPresentation.pulse}
                          tone={statusPresentation.tone}
                        />
                        {status === "failed" ? (
                          <span className="turn-status-detail">
                            {turnError?.message ?? "回答没有完成，请重试。"}
                          </span>
                        ) : null}
                        {status === "cancelled" ? (
                          <span className="turn-status-detail">
                            已生成的内容会保留。
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    {pendingApproval ? (
                      <div className="approval-prompt" role="group" aria-label="操作确认">
                        <strong>{pendingApproval.summary}</strong>
                        <p>{pendingApproval.reason}</p>
                        <div className="approval-actions">
                          <button
                            disabled={pendingAction !== null}
                            onClick={() =>
                              onResolveApproval(
                                turnSnapshot.turn.id,
                                pendingApproval.id,
                                "approve",
                              )
                            }
                            type="button"
                          >
                            允许一次
                          </button>
                          <button
                            disabled={pendingAction !== null}
                            onClick={() =>
                              onResolveApproval(
                                turnSnapshot.turn.id,
                                pendingApproval.id,
                                "deny",
                              )
                            }
                            type="button"
                          >
                            不允许
                          </button>
                        </div>
                      </div>
                    ) : null}
                    {persistedVariant.variant.finishReason === "length" ? (
                      <div className="turn-notice">回答达到长度上限，内容可能不完整。</div>
                    ) : null}

                    {isLatest &&
                    turnSnapshot.turn.conversationId ===
                      conversation?.conversation.id &&
                    !["created", "running"].includes(status) ? (
                      <div className="response-actions">
                        {status === "failed" || status === "cancelled" ? (
                          <button
                            disabled={pendingAction !== null}
                            onClick={() => onRetry(turnSnapshot.turn.id)}
                            type="button"
                          >
                            重试
                          </button>
                        ) : null}
                        {status === "completed" ? (
                          <button
                            disabled={pendingAction !== null}
                            onClick={() => onRegenerate(turnSnapshot.turn.id)}
                            type="button"
                          >
                            重新生成
                          </button>
                        ) : null}
                        {turnSnapshot.responseVariants.length > 1 ? (
                          <div className="variant-switcher" aria-label="回答版本">
                            <button
                              aria-label="上一个回答"
                              disabled={selectedIndex <= 0 || pendingAction !== null}
                              onClick={() =>
                                onSelectVariant(
                                  turnSnapshot.turn.id,
                                  turnSnapshot.responseVariants[selectedIndex - 1].variant.id,
                                )
                              }
                              type="button"
                            >
                              ‹
                            </button>
                            <span>
                              {selectedIndex + 1} / {turnSnapshot.responseVariants.length}
                            </span>
                            <button
                              aria-label="下一个回答"
                              disabled={
                                selectedIndex >= turnSnapshot.responseVariants.length - 1 ||
                                pendingAction !== null
                              }
                              onClick={() =>
                                onSelectVariant(
                                  turnSnapshot.turn.id,
                                  turnSnapshot.responseVariants[selectedIndex + 1].variant.id,
                                )
                              }
                              type="button"
                            >
                              ›
                            </button>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </article>

                {(() => {
                  const proposals = turnProposals(turnSnapshot.turn.id);
                  if (
                    !proposals.artifacts.length &&
                    !proposals.memories.length &&
                    !proposals.tasks.length &&
                    !proposals.knowledge.length
                  ) {
                    return null;
                  }
                  return (
                    <div className="proposal-stack">
                      {proposals.artifacts.map((proposal) => (
                        <ArtifactProposalCard
                          busy={proposalBusyId === proposal.id}
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onOpen={onOpenWorkspace}
                          onResolve={(decision) =>
                            onResolveArtifactProposal(proposal.id, decision)
                          }
                          proposal={proposal}
                          resolvedArtifact={resolvedArtifacts[proposal.id] ?? null}
                        />
                      ))}
                      {proposals.knowledge.map((proposal) => (
                        <KnowledgeProposalCard
                          busy={proposalBusyId === proposal.id}
                          conversationWorkspaceId={
                            conversation?.conversation.workspaceId ?? null
                          }
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onOpen={
                            onOpenAssistantTab
                              ? () => onOpenAssistantTab("knowledge")
                              : undefined
                          }
                          onResolve={(decision, workspaceId) =>
                            onResolveKnowledgeProposal(
                              proposal.id,
                              decision,
                              workspaceId,
                            )
                          }
                          proposal={proposal}
                          workspaces={workspaces ?? []}
                        />
                      ))}
                      {proposals.memories.map((proposal) => (
                        <MemoryProposalCard
                          busy={proposalBusyId === proposal.id}
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onOpen={
                            onOpenAssistantTab
                              ? () => onOpenAssistantTab("memory")
                              : undefined
                          }
                          onResolve={(decision) =>
                            onResolveMemoryProposal(proposal.id, decision)
                          }
                          proposal={proposal}
                        />
                      ))}
                      {proposals.tasks.map((proposal) => (
                        <TaskProposalCard
                          busy={proposalBusyId === proposal.id}
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onOpen={
                            onOpenAssistantTab
                              ? () => onOpenAssistantTab("scheduled")
                              : undefined
                          }
                          onResolve={(decision) =>
                            onResolveTaskProposal(proposal.id, decision)
                          }
                          proposal={proposal}
                        />
                      ))}
                    </div>
                  );
                })()}
              </section>
              </Fragment>
            );
          })}
          {hasInheritedTurns && firstOwnTurnIndex === -1 ? (
            <div className="lineage-divider" role="note">
              以上全部继承自《{conversation?.parentTitle ?? "主会话"}》，从这里开始是新内容
            </div>
          ) : null}
        </div>
      </div>

      <footer className="composer-region">
        <div className="composer">
          {variant !== "side" && conversation?.files.length ? (
            <div className="composer-files" aria-label="当前对话文件">
              {conversation.files.map((file) => (
                <span className="composer-file" key={file.id}>
                  <span aria-hidden="true">⌑</span>
                  <span title={file.originalName}>{file.originalName}</span>
                  <button
                    aria-label={`移除 ${file.originalName}`}
                    disabled={attachmentDisabled}
                    onClick={() => onRemoveFile(file.id)}
                    type="button"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <div className="composer-input-row">
            {variant === "side" ? null : (
              <>
                <input
                  ref={fileInputRef}
                  accept=".txt,.md,.markdown,.json,.csv,.tsv,.py,.js,.jsx,.ts,.tsx,.html,.css,.yaml,.yml,.toml"
                  className="file-input"
                  disabled={attachmentDisabled}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) onUploadFile(file);
                    event.target.value = "";
                  }}
                  type="file"
                />
                <button
                  aria-label="添加文本文件"
                  className="attach-button"
                  disabled={attachmentDisabled}
                  onClick={() => fileInputRef.current?.click()}
                  type="button"
                >
                  <span aria-hidden="true">＋</span>
                </button>
              </>
            )}
            <textarea
              aria-label="给 Endless 发送消息"
              ref={composerRef}
              disabled={composerDisabled || isGenerating}
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  onSend();
                }
              }}
              placeholder={
                archived
                  ? "恢复对话后继续"
                  : otherLaneRunning
                    ? `${runningLaneLabel}正在运行，请先查看或停止`
                    : providerUnavailable
                      ? "请先配置模型服务"
                      : variant === "side"
                        ? sideMode === "branch_lane"
                          ? "在此分支中继续对话"
                          : "在临时会话中发送消息"
                        : "给 Endless 发送消息"
              }
              rows={1}
              value={draft}
            />
            {isGenerating ? (
              <button
                aria-label="停止生成"
                className="send-button stop-button"
                disabled={pendingAction === "cancel"}
                onClick={onCancel}
                type="button"
              >
                <span aria-hidden="true" />
              </button>
            ) : (
              <button
                aria-label="发送消息"
                className="send-button"
                disabled={composerDisabled || !draft.trim() || pendingAction !== null}
                onClick={onSend}
                type="button"
              >
                ↑
              </button>
            )}
          </div>
        </div>
        <p className="composer-note">
          <span
            className={
              health?.providerConfigured
                ? "composer-status"
                : "composer-status is-warning"
            }
          >
            <span
              aria-hidden="true"
              className={
                health?.providerConfigured
                  ? "status-light"
                  : "status-light is-warning"
              }
            />
            <span
              aria-atomic="true"
              aria-live="polite"
              className="composer-status-text"
              role="status"
            >
              {health ? (health.providerConfigured ? "服务已连接" : "需要配置模型服务") : "本地服务未连接"}
            </span>
            <select
              aria-label="当前对话模型"
              className="model-select"
              disabled={!conversation || archived || pendingAction !== null}
              onChange={(event) => changeProvider(event.target.value)}
              value={conversation?.conversation.providerProfileId ?? ""}
            >
              <option value="">
                {defaultProvider
                  ? `默认 · ${defaultProvider.name} · ${defaultProvider.defaultModel}`
                  : "默认模型"}
              </option>
              {selectableProviders.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name} · {provider.defaultModel}
                </option>
              ))}
              <option value="__manage__">管理模型…</option>
            </select>
            <input
              aria-label="当前对话模型覆盖"
              className="model-input"
              disabled={!conversation || archived || pendingAction !== null}
              onBlur={commitModelDraft}
              onChange={(event) => setModelDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
              placeholder="模型"
              value={modelDraft}
            />
          </span>
          <span className="composer-hint">
            Enter 发送 · Shift + Enter 换行 · 可附加 UTF-8 文本（≤ 1 MB）
          </span>
        </p>
      </footer>

      {confirmingClose ? (
        <ConfirmDialog
          body={
            sideMode === "temporary_conversation"
              ? currentLaneHasActiveRun
                ? "将先停止当前运行，再删除整个临时对话。此操作无法撤销。"
                : "将删除整个临时对话。此操作无法撤销。"
              : "将停止当前运行并收起右侧工作面；分支和已有内容会保留。"
          }
          confirmLabel={
            sideMode === "temporary_conversation" ? "删除临时对话" : "停止并收起"
          }
          onClose={() => setConfirmingClose(false)}
          onConfirm={() => onCloseSide?.()}
          title={sideMode === "temporary_conversation" ? "关闭临时对话" : "收起运行中的分支"}
        />
      ) : null}

      {confirmingDelete ? (
        <ConfirmDialog
          body="永久删除这个对话？此操作无法撤销，该对话下的已安排事项与提醒也会一并取消。"
          confirmLabel="永久删除"
          onClose={() => setConfirmingDelete(false)}
          onConfirm={onDelete}
          title="删除对话"
        />
      ) : null}
    </SurfaceRoot>
  );
}

function EmptyConversation({ onSuggestion }: { onSuggestion: (value: string) => void }) {
  return (
    <section className="empty-conversation">
      <span className="empty-symbol">∞</span>
      <h2>今天想聊些什么？</h2>
      <p>从一个问题、一个想法，或一件想理清的事开始。</p>
      <div className="prompt-suggestions">
        <button onClick={() => onSuggestion("帮我梳理一下今天最重要的三件事")} type="button">
          帮我梳理今天最重要的事
        </button>
        <button onClick={() => onSuggestion("我有一个新想法，想和你一起推敲")} type="button">
          和我一起推敲一个想法
        </button>
        <button onClick={() => onSuggestion("帮我把这段内容整理成一篇可复用的文档")} type="button">
          把内容整理成一篇可复用文档
        </button>
      </div>
      <p className="empty-hint">在任意回答底部选择「从此处分叉」，可以保留当前上下文开始一次新的尝试。</p>
    </section>
  );
}

function LoadingState() {
  return (
    <div className="loading-state" aria-label="正在加载对话">
      <span />
      <span />
      <span />
    </div>
  );
}
