import { useEffect, useRef, useState } from "react";
import type {
  ArtifactRecordSummary,
  ConversationSnapshot,
  HealthSnapshot,
  LiveTurn,
  ResponseVariantSnapshot,
} from "./apiTypes";
import { ArtifactProposalCard } from "../proposals/ArtifactProposalCard";
import { MemoryProposalCard } from "../proposals/MemoryProposalCard";
import type { TurnProposals } from "../proposals/useProposals";
import { MessageContent } from "./MessageContent";

type ChatWorkSurfaceProps = {
  conversation: ConversationSnapshot | null;
  draft: string;
  error: string | null;
  health: HealthSnapshot | null;
  isGenerating: boolean;
  liveTurns: Record<string, LiveTurn>;
  loading: boolean;
  pendingAction: string | null;
  onArchive: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onDismissError: () => void;
  onDraftChange: (value: string) => void;
  onMenu: () => void;
  onOpenMemory: () => void;
  onRegenerate: (turnId: string) => void;
  onResolveApproval: (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny",
  ) => void;
  onRemoveFile: (fileId: string) => void;
  onRename: (title: string) => void;
  onRestore: () => void;
  onRetry: (turnId: string) => void;
  onSelectVariant: (turnId: string, variantId: string) => void;
  onSend: () => void;
  onUploadFile: (file: File) => void;
  proposalBusyId: string | null;
  proposalErrors: Record<string, string>;
  resolvedArtifacts: Record<string, ArtifactRecordSummary>;
  turnProposals: (turnId: string) => TurnProposals;
  onResolveArtifactProposal: (proposalId: string, decision: "accept" | "reject") => void;
  onResolveMemoryProposal: (proposalId: string, decision: "accept" | "reject") => void;
};

const statusText = {
  created: "准备回答",
  running: "正在回答",
  completed: "",
  failed: "回答失败",
  cancelled: "已停止",
} as const;

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
  onArchive,
  onCancel,
  onDelete,
  onDismissError,
  onDraftChange,
  onMenu,
  onOpenMemory,
  onRegenerate,
  onResolveApproval,
  onRemoveFile,
  onRename,
  onRestore,
  onRetry,
  onSelectVariant,
  onSend,
  onUploadFile,
  proposalBusyId,
  proposalErrors,
  resolvedArtifacts,
  turnProposals,
  onResolveArtifactProposal,
  onResolveMemoryProposal,
}: ChatWorkSurfaceProps) {
  const streamRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");

  const conversationId = conversation?.conversation.id;
  const latestTurnId = conversation?.turns.at(-1)?.turn.id;
  const latestLiveContent = latestTurnId ? liveTurns[latestTurnId]?.content : undefined;

  useEffect(() => {
    setRenaming(false);
    setTitleDraft(conversation?.conversation.title ?? "");
  }, [conversationId, conversation?.conversation.title]);

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: "smooth" });
  }, [conversation?.turns.length, latestLiveContent]);

  const submitTitle = () => {
    const title = titleDraft.trim();
    if (title && title !== conversation?.conversation.title) onRename(title);
    setRenaming(false);
  };

  const archived = conversation?.conversation.status === "archived";
  const providerUnavailable = health !== null && !health.providerConfigured;
  const composerDisabled = !conversation || archived || providerUnavailable;
  const attachmentDisabled =
    !conversation || archived || isGenerating || pendingAction !== null;

  return (
    <main className="chat-surface">
      <header className="surface-header">
        <button className="icon-button mobile-menu" onClick={onMenu} type="button">
          <span aria-hidden="true">☰</span>
          <span className="sr-only">打开会话列表</span>
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
          <span>{archived ? "已归档" : "私人对话"}</span>
        </div>
        <div className="surface-header-side">
          <div className="surface-global-actions">
            <button onClick={onOpenMemory} type="button">
              记忆
            </button>
          </div>
          {conversation ? (
            <div className="conversation-actions">
            <button onClick={() => setRenaming(true)} type="button">
              重命名
            </button>
            <button onClick={archived ? onRestore : onArchive} type="button">
              {archived ? "恢复" : "归档"}
            </button>
            <button
              className="danger-action"
              disabled={isGenerating}
              onClick={() => {
                if (globalThis.confirm("永久删除这个对话？此操作无法撤销。")) onDelete();
              }}
              type="button"
            >
              删除
            </button>
            </div>
          ) : null}
        </div>
      </header>

      <div className="conversation-stream" ref={streamRef}>
        {error ? (
          <div className="inline-error" role="alert">
            <span>{error}</span>
            <button onClick={onDismissError} type="button">关闭</button>
          </div>
        ) : null}

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
            const turnError = useLive ? live.error : undefined;
            const pendingApproval = useLive ? live.pendingApproval : undefined;
            const activities = useLive
              ? live.activities
              : (turnSnapshot.activities ?? []);
            const isLatest = turnIndex === conversation.turns.length - 1;
            const selectedIndex = turnSnapshot.responseVariants.findIndex(
              (item) => item.variant.id === persistedVariant.variant.id,
            );

            return (
              <section className="turn" key={turnSnapshot.turn.id}>
                <article className="message-row user-row">
                  <div className="speaker-mark user-mark">你</div>
                  <div className="user-copy">{turnSnapshot.userMessage.content}</div>
                </article>

                <article className="message-row assistant-row">
                  <div className="speaker-mark assistant-mark" aria-label="Endless">
                    ∞
                  </div>
                  <div className="assistant-content">
                    {content ? <MessageContent content={content} /> : null}
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
                    {status === "created" || status === "running" ? (
                      <div className="thinking-line">
                        <span className="thinking-dot" />
                        {pendingApproval ? "等待你的确认" : statusText[status]}
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
                    {status === "failed" ? (
                      <div className="turn-notice is-error">
                        {turnError?.message ?? "回答没有完成，请重试。"}
                      </div>
                    ) : null}
                    {status === "cancelled" ? (
                      <div className="turn-notice">回答已停止，已生成的内容会保留。</div>
                    ) : null}
                    {persistedVariant.variant.finishReason === "length" ? (
                      <div className="turn-notice">回答达到长度上限，内容可能不完整。</div>
                    ) : null}

                    {isLatest && !["created", "running"].includes(status) ? (
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
                  if (!proposals.artifacts.length && !proposals.memories.length) {
                    return null;
                  }
                  return (
                    <div className="proposal-stack">
                      {proposals.artifacts.map((proposal) => (
                        <ArtifactProposalCard
                          busy={proposalBusyId === proposal.id}
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onResolve={(decision) =>
                            onResolveArtifactProposal(proposal.id, decision)
                          }
                          proposal={proposal}
                          resolvedArtifact={resolvedArtifacts[proposal.id] ?? null}
                        />
                      ))}
                      {proposals.memories.map((proposal) => (
                        <MemoryProposalCard
                          busy={proposalBusyId === proposal.id}
                          error={proposalErrors[proposal.id] ?? null}
                          key={proposal.id}
                          onResolve={(decision) =>
                            onResolveMemoryProposal(proposal.id, decision)
                          }
                          proposal={proposal}
                        />
                      ))}
                    </div>
                  );
                })()}
              </section>
            );
          })}
        </div>
      </div>

      <footer className="composer-region">
        <div className="composer">
          {conversation?.files.length ? (
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
            <textarea
              aria-label="给 Endless 发送消息"
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
                  : providerUnavailable
                    ? "请先配置模型服务"
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
          Enter 发送 · Shift + Enter 换行 · 可附加 UTF-8 文本（≤ 1 MB）
        </p>
      </footer>
    </main>
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
      </div>
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
