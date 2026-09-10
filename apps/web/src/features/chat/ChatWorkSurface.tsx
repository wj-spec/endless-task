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
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  TurnStatus,
  Workspace,
  SkillInvocationCandidate,
} from "./apiTypes";
import { chatApi } from "./api";
import type { RuntimeConnectionPhase } from "./runtimeController";
import { CitationCard } from "./CitationCard";
import { RuntimeTracePanel } from "./RuntimeTracePanel";
import { ToolTraceGroup } from "./ToolTraceGroup";
import {
  buildRuntimeToolTrace,
  buildRunTimeline,
  groupTimeline,
  extractWorkspaceSourceRefs,
  type WorkspaceSourceRef,
} from "./runtimeTrace";
import {
  WorkspaceFilePreviewDrawer,
  WorkspaceRefList,
  type WorkspacePreviewTarget,
} from "./WorkspaceFilePreviewDrawer";
import { ArtifactProposalCard } from "../proposals/ArtifactProposalCard";
import { KnowledgeProposalCard } from "../proposals/KnowledgeProposalCard";
import { MemoryProposalCard } from "../proposals/MemoryProposalCard";
import { TaskProposalCard } from "../proposals/TaskProposalCard";
import type { TurnProposals } from "../proposals/useProposals";
import { MessageContent } from "./MessageContent";
import { ResponseFeedbackControl } from "./ResponseFeedbackControl";
import { CopyButton } from "./CopyButton";
import { PlanLine, type PlanPayload } from "./PlanLine";
import { StuckNotice } from "./StuckNotice";
import { deriveStuckState } from "./stuckState";
import { EscalationNotice } from "./EscalationNotice";
import { deriveEscalationState } from "./escalation";
import { VerificationBadge } from "./VerificationBadge";
import { deriveVerificationState } from "./verification";
import { UsageMeter } from "./UsageMeter";
import { UndoNotice } from "./UndoNotice";
import { AuditTrailPanel } from "./AuditTrailPanel";
import { runStageLabel } from "./runtimeStage";
import { GlobalSearchDialog } from "./GlobalSearchDialog";
import { SearchBar } from "./SearchBar";
import { useConversationSearch } from "./useConversationSearch";
import { friendlyLaneName } from "./laneLabel";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { RowMenu } from "../ui/RowMenu";
import { BranchIcon, ChevronIcon } from "../ui/Icons";
import { StatusBadge } from "../ui/StatusBadge";
import { ChatComposer } from "./ChatComposer";
import { ChatSurfaceHeader } from "./ChatSurfaceHeader";
import { SurfaceBanners } from "./SurfaceBanners";
import { RuntimeRecoveryNotices } from "./RuntimeRecoveryNotices";
import { AssistantMessageRow } from "./AssistantMessageRow";
import { EmptyConversation, LoadingState } from "./SurfaceStates";
import { StreamNotices } from "./StreamNotices";
import { UserMessageRow } from "./UserMessageRow";
import {
  APPROVAL_RISK_LABELS,
  approvalRisk,
  findActiveVariant,
  latestPlanForRun,
  turnStatusPresentation,
  type TurnStatusPresentation,
} from "./turnPresentation";
import { RuntimeStatusStrip } from "./RuntimeStatusStrip";
import { useTurnInteractions } from "./useTurnInteractions";
import { useTurnFocus } from "./useTurnFocus";
import { skillCommandsOf } from "../skills/skillCommand";

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
  runtimeEvents?: RuntimeV2ProductEvent[];
  runtimeSnapshot?: RuntimeV2Snapshot | null;
  /** 通知/引用跳转的定位目标：打开会话后滚动到这一轮并高亮。 */
  focusTarget?: { conversationId: string; turnId: string; nonce: number } | null;
  onArchive: () => void;
  onCancel: () => void;
  onCancelRunningRun?: (runId: string) => void;
  /** C2：运行卡住时把纠偏指令注入运行中的 steer 通道。 */
  onSteerRun?: (runId: string, content: string) => void;
  onCreateBranch?: (forkTurnId: string) => void;
  onCreateTemporaryConversation?: () => void;
  onDelete: () => void;
  onDismissError: () => void;
  onDraftChange: (value: string) => void;
  onMenu: () => void;
  onPromote: () => void;
  onRegenerate: (turnId: string) => void;
  onResolveApproval: (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny" | "modify",
    args?: Record<string, unknown>,
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
  onOpenConversation?: (
    conversationId: string,
    focusTurnId?: string | null,
  ) => void;
  onOpenRunningLane?: (laneId: string) => void;
  onOpenWorkspace?: () => void;
  onToggleWorkspace?: () => void;
  workspacePanelOpen?: boolean;
  /** S1：`/技能名` 候选。 */
  skillCandidates?: SkillInvocationCandidate[];
  /** S7：注入技能后聚焦输入框（值变化即聚焦）。 */
  composerFocusNonce?: number;
  onOpenWorkspaceSettings?: () => void;
  onEditResendMessage?: (turnId: string, content: string) => void;
  editedUserMessages?: Record<string, string>;
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
  runtimeEvents = [],
  focusTarget = null,
  runtimeSnapshot = null,
  onArchive,
  onCancel,
  onCancelRunningRun,
  onSteerRun,
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
  onToggleWorkspace,
  workspacePanelOpen,
  skillCandidates = [],
  composerFocusNonce = 0,
  onEditResendMessage,
  editedUserMessages,
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
  // G1 item 2: latest run_auto_restored event -> visible rollback notice.
  // C2 失败记忆：卡住态 = 快照 + 实时 run.stuck / run.progress_resumed 折叠。
  const stuckState = deriveStuckState(runtimeSnapshot, runtimeEvents);
  // C4 终止与升级：无进展/预算将尽时优先展示升级视图（含三条出路）。
  const escalationState = deriveEscalationState(runtimeSnapshot, runtimeEvents);
  // C1 独立验证：制造者产出后的检查者结论（验证中/通过/不确定/未通过）。
  const verificationState = deriveVerificationState(runtimeSnapshot, runtimeEvents);
  // C5 成本/延迟可见：当前 run 的 token/成本/耗时（成本为估算）。
  const usageState = runtimeSnapshot?.usage ?? null;
  const streamRef = useRef<HTMLDivElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [globalSearchOpen, setGlobalSearchOpen] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const search = useConversationSearch(conversation, liveTurns, streamRef);
  // 逐轮交互态（消息编辑 / 审批改参 / 引用卡片 / 文件预览）：收在 hook 里，
  // 下一轮拆 `TurnItem` 时可以把整个对象传下去。这里解构成原有变量名，使用点不变。
  const turnInteractions = useTurnInteractions(conversation?.conversation.id);
  const { resetForConversation } = turnInteractions;
  const {
    editTurnId,
    setEditTurnId,
    editDraft,
    setEditDraft,
    modifyApprovalId,
    setModifyApprovalId,
    modifyDraft,
    setModifyDraft,
    modifyError,
    setModifyError,
    citationsByTurn,
    openCitation,
    setOpenCitation,
    handleCitationClick,
    previewTarget,
    setPreviewTarget,
  } = turnInteractions;
  const [inheritedHistoryOpen, setInheritedHistoryOpen] = useState(false);


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
  const activeWorkspace =
    workspaces?.find(
      (item) => item.id === conversation?.conversation.workspaceId,
    ) ?? null;
  const currentLane = branchLanes.find((lane) => lane.id === currentLaneId) ?? null;
  const mainLane = branchLanes.find((lane) => lane.isMain) ?? null;
  const viewingBranch = variant === "main" && currentLane !== null && !currentLane.isMain;
  const currentLaneLabel = friendlyLaneName(currentLane);
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
        (item) =>
          item.turn.inherited !== true &&
          item.turn.conversationId === conversationId,
      )
    : -1;
  const inheritedTurnCount = (conversation?.turns ?? []).filter(
    (item) =>
      item.turn.inherited === true || item.turn.conversationId !== conversationId,
  ).length;
  const hasInheritedTurns = inheritedTurnCount > 0;
  const latestTurnId = conversation?.turns.at(-1)?.turn.id;
  const latestLiveContent = latestTurnId ? liveTurns[latestTurnId]?.content : undefined;

  useEffect(() => {
    setRenaming(false);
    setTitleDraft(conversation?.conversation.title ?? "");
    resetForConversation();
    setInheritedHistoryOpen(false);
  }, [conversationId, conversation?.conversation.title]);


  // 通知/引用跳转：定位目标轮次（定位期间暂停自动滚底，避免被顶掉）。
  const turnFocus = useTurnFocus({
    containerRef: streamRef,
    conversationId,
    focusTarget,
    turnCount: conversation?.turns.length ?? 0,
  });

  const submitTitle = () => {
    const title = titleDraft.trim();
    if (title && title !== conversation?.conversation.title) onRename(title);
    setRenaming(false);
  };

  const archived = conversation?.conversation.status === "archived";
  const runningLane = runtimeSnapshot?.runningLaneId
    ? branchLanes.find((lane) => lane.id === runtimeSnapshot.runningLaneId) ?? null
    : null;
  const otherLaneRunning = Boolean(
    runtimeSnapshot?.runningLaneId &&
      currentLaneId &&
      runtimeSnapshot.runningLaneId !== currentLaneId,
  );
  const runningLaneLabel = friendlyLaneName(runningLane, "另一分支");

  const activeSnapshot = runtimeSnapshot?.activeRunId && runtimeSnapshot.runState
    ? runtimeSnapshot
    : null;
  const runtimeTools = activeSnapshot
    ? buildRuntimeToolTrace(activeSnapshot.entries, activeSnapshot.toolStates)
    : [];
  // 17 切片③：该次回答用到的工作区文件来源（read/search 工具 → path+行）。
  const workspaceRefs = activeSnapshot
    ? extractWorkspaceSourceRefs(runtimeTools)
    : [];

  const SurfaceRoot = variant === "side" ? "section" : "main";

  return (
    <SurfaceRoot
      className={variant === "side" ? "chat-surface is-side" : "chat-surface"}
    >
      <div className="surface-top">
        <ChatSurfaceHeader
          activeWorkspace={activeWorkspace}
          archived={archived}
          branchLanes={branchLanes}
          conversation={conversation}
          currentLaneId={currentLaneId}
          currentLaneLabel={currentLaneLabel}
          isGenerating={isGenerating}
          pendingAction={pendingAction}
          renaming={renaming}
          sideMode={sideMode}
          titleDraft={titleDraft}
          variant={variant}
          onArchive={onArchive}
          onArchiveLane={onArchiveLane}
          onCreateBranch={onCreateBranch}
          onCreateTemporaryConversation={onCreateTemporaryConversation}
          onMenu={onMenu}
          onOpenLaneInSide={onOpenLaneInSide}
          onOpenWorkspaceSettings={onOpenWorkspaceSettings}
          onPromoteLane={onPromoteLane}
          onRenameLane={onRenameLane}
          onRequestSideClose={requestSideClose}
          onRestore={onRestore}
          onRestoreLane={onRestoreLane}
          onSearch={search.openSearch}
          onGlobalSearch={
            variant === "main" && onOpenConversation
              ? () => setGlobalSearchOpen(true)
              : undefined
          }
          onSetConfirmingDelete={() => setConfirmingDelete(true)}
          onSetRenaming={setRenaming}
          onShowArchivedLanes={onShowArchivedLanes}
          onSubmitTitle={submitTitle}
          onSwitchLane={onSwitchLane}
          onTitleDraftChange={setTitleDraft}
              onToggleWorkspace={onToggleWorkspace}
      workspacePanelOpen={workspacePanelOpen}
    />

        <SurfaceBanners
          canCancelRunningRun={Boolean(onCancelRunningRun)}
          canOpenRunningLane={Boolean(onOpenRunningLane)}
          canSwitchToMain={Boolean(mainLane && onSwitchLane)}
          currentLane={currentLane ?? null}
          currentLaneLabel={currentLaneLabel}
          ephemeral={conversation?.conversation.kind === "ephemeral"}
          mainLaneId={mainLane?.id ?? null}
          onCancelRunningRun={onCancelRunningRun}
          onOpenRunningLane={onOpenRunningLane}
          onPromote={onPromote}
          onPromoteLane={onPromoteLane}
          onSwitchLane={onSwitchLane}
          otherLaneRunning={otherLaneRunning}
          parentTitle={conversation?.parentTitle ?? null}
          pending={pendingAction !== null}
          runningLaneId={runtimeSnapshot?.runningLaneId ?? null}
          runningLaneLabel={runningLaneLabel}
          runningRunId={runtimeSnapshot?.runningRunId ?? null}
          viewingBranch={viewingBranch}
        />
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

      <div
        aria-atomic="false"
        aria-label="对话记录"
        aria-live="polite"
        aria-relevant="additions"
        className="conversation-stream"
        ref={streamRef}
        role="log"
      >
        <StreamNotices
          error={error}
          onDismissError={onDismissError}
          runtimeEvents={runtimeEvents}
        />

        <UndoNotice
          conversationId={conversationId ?? null}
          pending={pendingAction !== null}
          refreshKey={runtimeSnapshot?.lastEventSeq ?? null}
        />

        <RuntimeStatusStrip
          activeRunId={
            runtimeSnapshot?.activeRunId ??
            runtimeSnapshot?.runningRunId ??
            null
          }
          escalationState={escalationState}
          lastEventSeq={runtimeSnapshot?.lastEventSeq ?? null}
          onCancelRunningRun={onCancelRunningRun}
          onSteerRun={onSteerRun}
          pending={pendingAction !== null}
          stuckState={stuckState}
          usage={usageState}
          verificationState={verificationState}
        />

        <RuntimeRecoveryNotices
          connectionPhase={runtimeConnectionPhase}
          onResolveRuntimeRecovery={onResolveRuntimeRecovery}
          pending={pendingAction !== null}
          runtimeSnapshot={runtimeSnapshot ?? null}
        />

        {loading && !conversation ? <LoadingState /> : null}
        {!loading && conversation?.turns.length === 0 ? (
          <EmptyConversation onSuggestion={onDraftChange} />
        ) : null}

        <div className="message-column">
          {hasInheritedTurns ? (
            <section className="lineage-summary" role="note">
              <div>
                <strong>继承自《{conversation?.parentTitle ?? "主会话"}》</strong>
                <span>{inheritedTurnCount} 轮上下文已随临时会话复制并隔离保存</span>
              </div>
              <button
                aria-expanded={inheritedHistoryOpen}
                onClick={() => setInheritedHistoryOpen((open) => !open)}
                type="button"
              >
                {inheritedHistoryOpen ? "收起继承内容" : "查看继承内容"}
              </button>
            </section>
          ) : null}
          {conversation?.turns.map((turnSnapshot, turnIndex) => {
            const inheritedTurn =
              turnSnapshot.turn.inherited === true ||
              turnSnapshot.turn.conversationId !== conversationId;
            if (inheritedTurn && !inheritedHistoryOpen) return null;
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
            // 该轮次真实注入的引用标签集合：命中 → 可点击引用；未命中 → 占位/无法溯源。
            const resolvableCitationLabels = new Set(
              (citationsByTurn[turnSnapshot.turn.id] ?? []).map(
                (citation) => citation.label,
              ),
            );
            // 兜底：run 已进入终态时，不让「正在回答」徽章因快照/事件同步滞后而卡住。
            const runState = runtimeSnapshot?.runState;
            const terminalRunStatus = runState
              ? runState.status === "completed"
                ? "completed"
                : runState.status === "failed"
                  ? "failed"
                  : runState.status === "cancelled"
                    ? "cancelled"
                    : null
              : null;
            const finalStatus = terminalRunStatus ?? status;
            const pendingApproval = useLive ? live.pendingApproval : undefined;
            // 本对话车道是否正有运行（用于禁用跨轮操作，避免与后端 409 冲突）
            const laneBusy = Boolean(
              runState && !["completed", "failed", "cancelled"].includes(runState.status),
            );
            const statusPresentation = turnStatusPresentation(
              finalStatus,
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
            // 按时间流交错（文本片段 + 工具卡）。仅最新、且能拿到该 run 事件序列且出现工具时启用。
            const timeline = isLatest
              ? buildRunTimeline(runtimeEvents, runtimeTools, turnSnapshot.turn.id)
              : null;

            const dividerHere =
              hasInheritedTurns &&
              inheritedHistoryOpen &&
              turnIndex === firstOwnTurnIndex;
            return (
              <Fragment key={turnSnapshot.turn.id}>
              {dividerHere ? (
                <div className="lineage-divider" role="note">
                  以上继承自《{conversation.parentTitle ?? "主会话"}》，以下是本会话内容
                </div>
              ) : null}
              <section
                className="turn"
                data-turn-id={turnSnapshot.turn.id}
                data-variant-ids={turnSnapshot.responseVariants
                  .map((item) => item.variant.id)
                  .join(" ")}
              >
                <UserMessageRow
                  belongsToConversation={
                    turnSnapshot.turn.conversationId ===
                    conversation?.conversation.id
                  }
                  content={turnSnapshot.userMessage.content}
                  editDraft={editDraft}
                  editedContent={editedUserMessages?.[turnSnapshot.turn.id]}
                  editing={editTurnId === turnSnapshot.turn.id}
                  isGenerating={isGenerating}
                  isLatest={isLatest}
                  laneBusy={laneBusy}
                  onCancelEdit={() => setEditTurnId(null)}
                  onEditDraftChange={setEditDraft}
                  onEditResend={onEditResendMessage}
                  onStartEdit={(value) => {
                    setEditTurnId(turnSnapshot.turn.id);
                    setEditDraft(value);
                  }}
                  pending={pendingAction !== null}
                  status={status}
                  turnId={turnSnapshot.turn.id}
                />

                <AssistantMessageRow
                  activeSnapshot={activeSnapshot}
                  activities={activities}
                  content={content}
                  conversation={conversation}
                  isGenerating={isGenerating}
                  isLatest={isLatest}
                  jumpCitation={jumpCitation}
                  laneBusy={laneBusy}
                  onCitationClick={(tid, label) => {
                    void handleCitationClick(tid, label);
                  }}
                  onCreateBranch={onCreateBranch}
                  onRegenerate={onRegenerate}
                  onResolveApproval={onResolveApproval}
                  onResolveArtifactProposal={onResolveArtifactProposal}
                  onResolveKnowledgeProposal={onResolveKnowledgeProposal}
                  onResolveMemoryProposal={onResolveMemoryProposal}
                  onResolveTaskProposal={onResolveTaskProposal}
                  onRetry={onRetry}
                  onSelectVariant={onSelectVariant}
                  pendingAction={pendingAction}
                  pendingApproval={pendingApproval}
                  persistedVariant={persistedVariant}
                  proposalBusyId={proposalBusyId}
                  proposalErrors={proposalErrors}
                  resolvableCitationLabels={resolvableCitationLabels}
                  resolvedArtifacts={resolvedArtifacts}
                  responseVariants={turnSnapshot.responseVariants}
                  selectedIndex={selectedIndex}
                  turnError={turnError}
                  runtimeEvents={runtimeEvents}
                  runtimeSnapshot={runtimeSnapshot}
                  runtimeTools={runtimeTools}
                  status={status}
                  timeline={timeline}
                  turnId={turnSnapshot.turn.id}
                  turnProposals={turnProposals}
                  variant={variant}
                  workspaceRefs={workspaceRefs}
                  interactions={turnInteractions}
                />

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
                          data-conversation-workspace-id={
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
          {hasInheritedTurns &&
          inheritedHistoryOpen &&
          firstOwnTurnIndex === -1 ? (
            <div className="lineage-divider" role="note">
              以上全部继承自《{conversation?.parentTitle ?? "主会话"}》，从这里开始是新内容
            </div>
          ) : null}
        </div>
      </div>

      <ChatComposer
        contextBudget={runtimeSnapshot?.contextBudget}
        conversation={conversation}
        draft={draft}
        health={health}
        isGenerating={isGenerating}
        loading={loading}
        otherLaneRunning={otherLaneRunning}
        pendingAction={pendingAction}
        providers={providers}
        runningLaneLabel={runningLaneLabel}
        sideMode={sideMode}
        variant={variant}
        onCancel={onCancel}
        onDraftChange={onDraftChange}
        onModelChange={onModelChange}
        onOpenProviders={() => onOpenAssistantTab?.("providers")}
        onRemoveFile={onRemoveFile}
        onSend={onSend}
        onUploadFile={onUploadFile}
        skillCandidates={skillCandidates}
        focusNonce={composerFocusNonce}
        steerable={currentLaneHasActiveRun}
      />

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

      {globalSearchOpen ? (
        <GlobalSearchDialog
          onClose={() => setGlobalSearchOpen(false)}
          onOpenAssistantTab={(tab) => {
            setGlobalSearchOpen(false);
            onOpenAssistantTab?.(tab);
          }}
          onOpenConversation={(conversationId) => {
            setGlobalSearchOpen(false);
            onOpenConversation?.(conversationId);
          }}
          onOpenWorkspace={() => {
            setGlobalSearchOpen(false);
            onOpenWorkspace?.();
          }}
        />
      ) : null}
    </SurfaceRoot>
  );
}
