import type {
  ActivitySnapshot,
  ConversationSnapshot,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  ResponseVariantSnapshot,
  TurnStatus,
} from "./apiTypes";
import { CitationCard } from "./CitationCard";
import { MessageContent } from "./MessageContent";
import { RuntimeTracePanel } from "./RuntimeTracePanel";
import { ToolTraceGroup } from "./ToolTraceGroup";
import { buildRunTimeline, groupTimeline } from "./runtimeTrace";
import type { WorkspaceSourceRef } from "./runtimeTrace";
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
import { ResponseFeedbackControl } from "./ResponseFeedbackControl";
import { CopyButton } from "./CopyButton";
import { PlanLine, type PlanPayload } from "./PlanLine";
import { runStageLabel } from "./runtimeStage";
import { UsageMeter } from "./UsageMeter";
import { friendlyLaneName } from "./laneLabel";
import {
  APPROVAL_RISK_LABELS,
  approvalRisk,
  latestPlanForRun,
  turnStatusPresentation,
} from "./turnPresentation";
import { StatusBadge } from "../ui/StatusBadge";
import { RowMenu } from "../ui/RowMenu";
import { BranchIcon, ChevronIcon } from "../ui/Icons";
import type { TurnInteractions } from "./useTurnInteractions";

/**
 * 助手消息行：阶段行、时间流/正文、引用卡片、工作区来源与预览抽屉、操作条，
 * 以及等待审批时的三条出路（批准 / 拒绝 / 改参数重试）。
 *
 * 从 `ChatWorkSurface.tsx`（1 241 行）的轮次渲染块整块搬出（399 行，行为零改动）。
 * 搬出方式沿用 `UserMessageRow` 那一刀的经验：**一次只搬一块、先有护栏再动刀**、
 * 依赖全部显式化成 props（36 个外层名字 → 20 个 props + `interactions` 对象）。
 *
 * 轮次级交互态（引用卡片、文件预览、审批改参）仍由 `useTurnInteractions` 持有；
 * 本组件不持有状态，只做渲染与回调。
 */
export type AssistantMessageRowProps = {
  turnId: string;
  persistedVariant: ResponseVariantSnapshot;
  responseVariants: ResponseVariantSnapshot[];
  content: string;
  status: TurnStatus;
  /** 等待审批的审批对象（`live.pendingApproval`），没有则 undefined。 */
  pendingApproval?: import("./apiTypes").LiveTurn["pendingApproval"];
  timeline: ReturnType<typeof buildRunTimeline>;
  resolvableCitationLabels: Set<string>;
  activities: ActivitySnapshot[];
  selectedIndex: number;
  turnError?: import("./apiTypes").LiveTurn["error"];
  isLatest: boolean;
  laneBusy: boolean;
  isGenerating: boolean;
  /** 主命令的进行中动作名（与 ChatWorkSurface 的 `pendingAction` 同义）。 */
  pendingAction: string | null;
  variant: "main" | "side";
  conversation: ConversationSnapshot | null;
  /** 该轮次真实注入的引用（来自 `useTurnInteractions`）。 */
  runtimeSnapshot: RuntimeV2Snapshot | null | undefined;
  runtimeEvents: RuntimeV2ProductEvent[] | undefined;
  runtimeTools: Parameters<typeof RuntimeTracePanel>[0]["tools"];
  activeSnapshot: RuntimeV2Snapshot | null;
  workspaceRefs: WorkspaceSourceRef[];
  turnProposals: (turnId: string) => TurnProposals;
  proposalBusyId: string | null;
  proposalErrors: Record<string, string>;
  resolvedArtifacts: Record<string, unknown>;
  onRegenerate: (turnId: string) => void;
  onRetry: (turnId: string) => void;
  onSelectVariant: (turnId: string, variantId: string) => void;
  onCreateBranch?: (forkTurnId: string) => void;
  onResolveApproval: (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny" | "modify",
    args?: Record<string, unknown>,
  ) => void;
  onResolveArtifactProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onResolveKnowledgeProposal: (
    proposalId: string,
    decision: "accept" | "reject",
    workspaceId?: string | null,
  ) => void;
  onResolveMemoryProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onResolveTaskProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onCitationClick: (turnId: string, label: string) => void;
  jumpCitation: (citation: import("./apiTypes").KnowledgeCitation) => void;
  interactions: TurnInteractions;
};

export function AssistantMessageRow({
  turnId,
  persistedVariant,
  responseVariants,
  content,
  status,
  pendingApproval,
  timeline,
  resolvableCitationLabels,
  activities,
  selectedIndex,
  turnError,
  isLatest,
  laneBusy,
  isGenerating,
  pendingAction,
  runtimeEvents,
  variant,
  conversation,
  runtimeSnapshot,
  runtimeTools,
  activeSnapshot,
  workspaceRefs,
  turnProposals,
  proposalBusyId,
  proposalErrors,
  resolvedArtifacts,
  onRegenerate,
  onRetry,
  onSelectVariant,
  onCreateBranch,
  onResolveApproval,
  onResolveArtifactProposal,
  onResolveKnowledgeProposal,
  onResolveMemoryProposal,
  onResolveTaskProposal,
  onCitationClick,
  jumpCitation,
  interactions,
}: AssistantMessageRowProps) {
  const {
    citationsByTurn,
    openCitation,
    setOpenCitation,
    previewTarget,
    setPreviewTarget,
    modifyApprovalId,
    setModifyApprovalId,
    modifyDraft,
    setModifyDraft,
    modifyError,
    setModifyError,
    editDraft: _editDraft,
    setEditDraft: _setEditDraft,
    editTurnId: _editTurnId,
    setEditTurnId: _setEditTurnId,
    resetForConversation: _resetForConversation,
  } = interactions;
  // 这两项在本块里只作为 `void` 占位，避免未使用变量告警。
  void _editDraft; void _setEditDraft; void _editTurnId; void _setEditTurnId;
  void _resetForConversation;
  // 与 `ChatWorkSurface` 的 map 里同表达式（终态兜底 + 等待审批优先）。
  const pending = pendingAction !== null;
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
  const finalStatus: TurnStatus = terminalRunStatus ?? status;
  const statusPresentation = turnStatusPresentation(
    finalStatus,
    Boolean(pendingApproval),
  );

  return (
  <article className="message-row assistant-row">
    <div className="speaker-mark assistant-mark" aria-label="Endless">
      ∞
    </div>
    <div className="assistant-content">
      {(() => {
        if (!isLatest) return null;
        const pending = pendingAction !== null;
  const runState = runtimeSnapshot?.runState;
        const running =
          runState?.runId === turnId &&
          Boolean(runState.status) &&
          !["completed", "failed", "cancelled"].includes(
            runState.status,
          );
        const stage = runStageLabel({
          runId: turnId,
          running,
          events: runtimeEvents,
          plan: isLatest
            ? latestPlanForRun(runtimeSnapshot, turnId)
            : null,
        });
        return stage ? (
          <p aria-live="polite" className="activity-stage">
            {stage}
          </p>
        ) : null;
      })()}
      {timeline ? (
        <div
          className="turn-timeline"
          aria-label="执行时间流"
          aria-live={status === "created" || status === "running" ? "polite" : undefined}
        >
          {groupTimeline(timeline).map((group) =>
            group.kind === "text" ? (
              <div className="timeline-text" key={group.key}>
                <MessageContent
                  content={group.text}
                  resolvableCitationLabels={resolvableCitationLabels}
                />
              </div>
            ) : (
              <ToolTraceGroup
                active={status === "created" || status === "running"}
                key={group.key}
                tools={group.tools}
              />
            ),
          )}
        </div>
      ) : content ? (
        <div
          className={
            status === "created" || status === "running"
              ? "message-streaming"
              : undefined
          }
        >
          <MessageContent
            content={content}
            onCitationClick={(label) =>
              onCitationClick(turnId, label)
            }
            resolvableCitationLabels={resolvableCitationLabels}
          />
        </div>
      ) : null}
      {openCitation?.turnId === turnId ? (
        <CitationCard
          citation={
            (citationsByTurn[turnId] ?? []).find(
              (item) => item.label === openCitation.label,
            ) ?? null
          }
          jumpDisabled={variant === "side"}
          onClose={() => setOpenCitation(null)}
          onJump={jumpCitation}
        />
      ) : null}
      {workspaceRefs.length > 0 &&
      isLatest &&
      variant === "main" ? (
        <WorkspaceRefList
          refs={workspaceRefs}
          workspaceId={
            conversation?.conversation.workspaceId ?? ""
          }
          onOpen={(ref: WorkspaceSourceRef) => {
            const workspaceId =
              conversation?.conversation.workspaceId ?? "";
            if (!workspaceId) return;
            setPreviewTarget({
              workspaceId,
              path: ref.path,
              startLine: ref.startLine,
              endLine: ref.endLine,
            });
          }}
        />
      ) : null}
      {previewTarget && isLatest && variant === "main" ? (
        <WorkspaceFilePreviewDrawer
          target={previewTarget}
          onClose={() => setPreviewTarget(null)}
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
        <div
          className={`approval-prompt is-${
            approvalRisk(pendingApproval.metadata)
          }`}
          role="group"
          aria-label="操作确认"
        >
          <div className="approval-head">
            <span className="approval-risk-badge">
              {APPROVAL_RISK_LABELS[approvalRisk(pendingApproval.metadata)]}
            </span>
            <strong className="approval-summary">
              {pendingApproval.summary}
            </strong>
          </div>
          <p className="approval-reason">{pendingApproval.reason}</p>
          <details className="approval-context">
            <summary>为什么？</summary>
            <div className="approval-context-body">
              工具：
              {String(pendingApproval.metadata?.toolName ?? "未知")}
              {pendingApproval.metadata?.effect
                ? ` · 影响范围：${String(pendingApproval.metadata.effect)}`
                : ""}
            </div>
          </details>
          <div className="approval-actions">
            <button
              className="approval-allow"
              disabled={pendingAction !== null}
              onClick={() =>
                onResolveApproval(
                  turnId,
                  pendingApproval.id,
                  "approve",
                )
              }
              type="button"
            >
              允许一次
            </button>
            <button
              className="approval-deny"
              disabled={pendingAction !== null}
              onClick={() =>
                onResolveApproval(
                  turnId,
                  pendingApproval.id,
                  "deny",
                )
              }
              type="button"
            >
              不允许
            </button>
            {approvalRisk(pendingApproval.metadata) !== "high" ? (
              <button
                className="approval-modify"
                disabled={pendingAction !== null}
                onClick={() => {
                  setModifyApprovalId(pendingApproval.id);
                  setModifyDraft("{}");
                  setModifyError(null);
                }}
                type="button"
              >
                修改参数
              </button>
            ) : null}
          </div>
          {modifyApprovalId === pendingApproval.id ? (
            <div
              className="approval-modify-panel"
              role="group"
              aria-label="修改工具参数"
            >
              <span className="approval-modify-label">修改参数（JSON）</span>
              <textarea
                aria-label="修改工具参数"
                className="approval-modify-input"
                onChange={(event) => setModifyDraft(event.target.value)}
                rows={4}
                spellCheck={false}
                value={modifyDraft}
              />
              {modifyError ? (
                <span className="approval-modify-error">{modifyError}</span>
              ) : null}
              <div className="approval-modify-actions">
                <button
                  className="approval-modify-submit"
                  disabled={pendingAction !== null}
                  onClick={() => {
                    try {
                      const parsed = JSON.parse(modifyDraft);
                      if (
                        !parsed ||
                        typeof parsed !== "object" ||
                        Array.isArray(parsed)
                      ) {
                        setModifyError("参数必须是 JSON 对象。");
                        return;
                      }
                      onResolveApproval(
                        turnId,
                        pendingApproval.id,
                        "modify",
                        parsed,
                      );
                      setModifyApprovalId(null);
                    } catch {
                      setModifyError("JSON 解析失败，请检查格式。");
                    }
                  }}
                  type="button"
                >
                  确认修改并执行
                </button>
                <button
                  className="approval-modify-cancel"
                  disabled={pendingAction !== null}
                  onClick={() => setModifyApprovalId(null)}
                  type="button"
                >
                  取消
                </button>
              </div>
            </div>
          ) : null}
          <span className="approval-timeout">
            不响应则暂停，不会执行。
          </span>
        </div>
      ) : null}
      {persistedVariant.variant.finishReason === "length" ? (
        <div className="turn-notice">回答达到长度上限，内容可能不完整。</div>
      ) : null}

      {(() => {
        const plan = isLatest
          ? latestPlanForRun(runtimeSnapshot, turnId)
          : null;
        return plan ? (
          <PlanLine
            active={["created", "running"].includes(status)}
            busy={laneBusy}
            plan={plan}
          />
        ) : null;
      })()}
      {conversation?.conversation.id ===
        conversation?.conversation.id &&
      (status === "completed" ||
        (isLatest && (status === "failed" || status === "cancelled"))) ? (
        <div className="response-actions">
          {content.length > 0 ? (
            <CopyButton ariaLabel="复制这段回答" text={content} />
          ) : null}
          {status === "completed" && content.length > 0 ? (
            <ResponseFeedbackControl
              conversationId={conversation?.conversation.id ?? null}
              disabled={
                pendingAction !== null || laneBusy || isGenerating
              }
              turnId={turnId}
              variantId={persistedVariant.variant.id}
            />
          ) : null}
          {isLatest && (status === "failed" || status === "cancelled") ? (
            <button
              disabled={pendingAction !== null}
              onClick={() => onRetry(turnId)}
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
              onClick={() => onRegenerate(turnId)}
              type="button"
            >
              重新生成
            </button>
          ) : null}
          {isLatest &&
          responseVariants.length > 1 &&
          status === "completed" ? (
            <div className="variant-switcher" aria-label="其他回答">
              <button
                aria-label="上一个回答"
                disabled={
                  selectedIndex <= 0 ||
                  pendingAction !== null ||
                  laneBusy
                }
                onClick={() =>
                  onSelectVariant(
                    turnId,
                    responseVariants[selectedIndex - 1]
                      .variant.id,
                  )
                }
                type="button"
              >
                <ChevronIcon direction="left" size={16} />
              </button>
              <span>
                {selectedIndex + 1} / {responseVariants.length}
              </span>
              <button
                aria-label="下一个回答"
                disabled={
                  selectedIndex >=
                    responseVariants.length - 1 ||
                  pendingAction !== null ||
                  laneBusy
                }
                onClick={() =>
                  onSelectVariant(
                    turnId,
                    responseVariants[selectedIndex + 1]
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
              onClick={() => onCreateBranch(turnId)}
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
  );
}
