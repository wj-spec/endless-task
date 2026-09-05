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
  RuntimeV2RecoveryReport,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  TurnStatus,
  Workspace,
} from "./apiTypes";
import { chatApi } from "./api";
import type { RuntimeConnectionPhase } from "./runtimeController";
import { CitationCard } from "./CitationCard";
import { RuntimeTracePanel, RuntimeToolCard } from "./RuntimeTracePanel";
import { buildRuntimeToolTrace, buildRunTimeline } from "./runtimeTrace";
import { ArtifactProposalCard } from "../proposals/ArtifactProposalCard";
import { KnowledgeProposalCard } from "../proposals/KnowledgeProposalCard";
import { MemoryProposalCard } from "../proposals/MemoryProposalCard";
import { TaskProposalCard } from "../proposals/TaskProposalCard";
import type { TurnProposals } from "../proposals/useProposals";
import { CollapsibleMessage } from "./CollapsibleMessage";
import { CopyButton } from "./CopyButton";
import { SearchBar } from "./SearchBar";
import { useConversationSearch } from "./useConversationSearch";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { RowMenu } from "../ui/RowMenu";
import { BranchIcon, ChevronIcon } from "../ui/Icons";
import { StatusBadge } from "../ui/StatusBadge";
import { ChatComposer } from "./ChatComposer";
import { ChatSurfaceHeader } from "./ChatSurfaceHeader";

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
  onArchive: () => void;
  onCancel: () => void;
  onCancelRunningRun?: (runId: string) => void;
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

const recoveryPresentation = (report: RuntimeV2RecoveryReport) => {
  const reasons = new Set(report.findings.map((finding) => finding.reason));
  if (
    reasons.has("waiting_approval") ||
    reasons.has("tool_side_effect_uncertain") ||
    reasons.has("tool_result_missing")
  ) {
    return {
      title: "上次操作需要确认",
      message: "应用中断时可能正在执行工具。请先检查相关结果，再结束这次运行。",
      canRetry: false,
    };
  }
  if (reasons.has("cancellation_pending")) {
    return {
      title: "上次停止操作未完成",
      message: "应用在停止运行时中断，请结束这次运行后再继续。",
      canRetry: false,
    };
  }
  if (report.classification === "non_recoverable") {
    return {
      title: "上次运行无法恢复",
      message: "运行记录不完整，请结束这次运行后重新发送消息。",
      canRetry: false,
    };
  }
  if (reasons.has("interrupted_model_turn")) {
    return {
      title: "上一条回复未完成",
      message: "应用在生成回复时中断，可以重新生成这条回复。",
      canRetry: report.action === "resume",
    };
  }
  return {
    title: "上次运行未完成",
    message: "应用在运行完成前中断，可以重新尝试或结束这次运行。",
    canRetry:
      report.classification === "recoverable" && report.action === "resume",
  };
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
  runtimeEvents = [],
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
  const [inheritedHistoryOpen, setInheritedHistoryOpen] = useState(false);

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
    const items = citationsByTurn[turnId] ?? [];
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
  const activeWorkspace =
    workspaces?.find(
      (item) => item.id === conversation?.conversation.workspaceId,
    ) ?? null;
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
    setOpenCitation(null);
    setInheritedHistoryOpen(false);
  }, [conversationId, conversation?.conversation.title]);

  useEffect(() => {
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "auto"
      : "smooth";
    streamRef.current?.scrollTo({
      top: streamRef.current.scrollHeight,
      behavior,
    });
  }, [conversation?.turns.length, latestLiveContent]);

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
  const runningLaneLabel =
    runningLane?.displayName ?? runningLane?.title ?? runningLane?.summary ?? "另一分支";

  const activeSnapshot = runtimeSnapshot?.activeRunId && runtimeSnapshot.runState
    ? runtimeSnapshot
    : null;
  const runtimeTools = activeSnapshot
    ? buildRuntimeToolTrace(activeSnapshot.entries, activeSnapshot.toolStates)
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
          onSetConfirmingDelete={() => setConfirmingDelete(true)}
          onSetRenaming={setRenaming}
          onShowArchivedLanes={onShowArchivedLanes}
          onSubmitTitle={submitTitle}
          onSwitchLane={onSwitchLane}
          onTitleDraftChange={setTitleDraft}
        />

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

      <div
        aria-atomic="false"
        aria-label="对话记录"
        aria-live="polite"
        aria-relevant="additions"
        className="conversation-stream"
        ref={streamRef}
        role="log"
      >
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
          ? runtimeSnapshot?.interruptedRuns.map((report) => {
              const presentation = recoveryPresentation(report);
              return (
                <section
                  className="runtime-recovery-card"
                  key={report.runId}
                  role="alert"
                >
                  <strong>{presentation.title}</strong>
                  <p>{presentation.message}</p>
                  <div className="runtime-recovery-actions">
                    {presentation.canRetry ? (
                      <button
                        disabled={pendingAction !== null}
                        onClick={() => onResolveRuntimeRecovery(report.runId, "retry")}
                        type="button"
                      >
                        重新生成
                      </button>
                    ) : null}
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
              );
            })
          : null}

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
              <section className="turn">
                <article className="message-row user-row">
                  <div className="speaker-mark user-mark">你</div>
                  <div className="user-row-body">
                    <div className="user-copy">{turnSnapshot.userMessage.content}</div>
                    <div className="user-row-actions">
                      <CopyButton
                        ariaLabel="复制这条消息"
                        text={turnSnapshot.userMessage.content}
                      />
                    </div>
                  </div>
                </article>

                <article className="message-row assistant-row">
                  <div className="speaker-mark assistant-mark" aria-label="Endless">
                    ∞
                  </div>
                  <div className="assistant-content">
                    {timeline ? (
                      <div
                        className="turn-timeline"
                        aria-label="执行时间流"
                        aria-live={status === "created" || status === "running" ? "polite" : undefined}
                      >
                        {timeline.map((item) =>
                          item.kind === "text" ? (
                            <p className="timeline-text" key={item.id}>
                              {item.text}
                            </p>
                          ) : (
                            <RuntimeToolCard key={item.id} tool={item.tool} />
                          ),
                        )}
                      </div>
                    ) : content ? (
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
                    {!timeline && activeSnapshot && isLatest ? (
                      <RuntimeTracePanel tools={runtimeTools} />
                    ) : !timeline && activities.length ? (
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

                    {turnSnapshot.turn.conversationId ===
                      conversation?.conversation.id &&
                    (status === "completed" ||
                      (isLatest && (status === "failed" || status === "cancelled"))) ? (
                      <div className="response-actions">
                        {content.length > 0 ? (
                          <CopyButton ariaLabel="复制这段回答" text={content} />
                        ) : null}
                        {isLatest && (status === "failed" || status === "cancelled") ? (
                          <button
                            disabled={pendingAction !== null}
                            onClick={() => onRetry(turnSnapshot.turn.id)}
                            type="button"
                          >
                            重试
                          </button>
                        ) : null}
                        {isLatest && status === "completed" ? (
                          <button
                            disabled={
                              pendingAction !== null || laneBusy || isGenerating
                            }
                            onClick={() => onRegenerate(turnSnapshot.turn.id)}
                            type="button"
                          >
                            重新生成
                          </button>
                        ) : null}
                        {isLatest &&
                        turnSnapshot.responseVariants.length > 1 &&
                        status === "completed" ? (
                          <div className="variant-switcher" aria-label="回答版本">
                            <button
                              aria-label="上一个回答"
                              disabled={
                                selectedIndex <= 0 ||
                                pendingAction !== null ||
                                laneBusy
                              }
                              onClick={() =>
                                onSelectVariant(
                                  turnSnapshot.turn.id,
                                  turnSnapshot.responseVariants[selectedIndex - 1]
                                    .variant.id,
                                )
                              }
                              type="button"
                            >
                              <ChevronIcon direction="left" size={16} />
                            </button>
                            <span>
                              {selectedIndex + 1} / {turnSnapshot.responseVariants.length}
                            </span>
                            <button
                              aria-label="下一个回答"
                              disabled={
                                selectedIndex >=
                                  turnSnapshot.responseVariants.length - 1 ||
                                pendingAction !== null ||
                                laneBusy
                              }
                              onClick={() =>
                                onSelectVariant(
                                  turnSnapshot.turn.id,
                                  turnSnapshot.responseVariants[selectedIndex + 1]
                                    .variant.id,
                                )
                              }
                              type="button"
                            >
                              <ChevronIcon direction="right" size={16} />
                            </button>
                          </div>
                        ) : null}
                        {status === "completed" && onCreateBranch ? (
                          <button
                            aria-busy={pendingAction === "fork-lane"}
                            aria-label="从此回答创建分支"
                            className="branch-from-answer"
                            disabled={isGenerating || pendingAction !== null || laneBusy}
                            onClick={() => onCreateBranch(turnSnapshot.turn.id)}
                            title="保留到这条完整回答，在右侧开始分支"
                            type="button"
                          >
                            <BranchIcon size={16} />
                            {pendingAction === "fork-lane" ? "正在创建…" : "创建分支"}
                          </button>
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
    </SurfaceRoot>
  );
}

function EmptyConversation({ onSuggestion }: { onSuggestion: (value: string) => void }) {
  return (
    <section className="empty-conversation">
      <span aria-hidden="true" className="empty-symbol">∞</span>
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
