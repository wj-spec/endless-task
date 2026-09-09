import { useId, useState } from "react";
import type { ArtifactProposal, ArtifactRecordSummary } from "../chat/apiTypes";
import { MessageContent } from "../chat/MessageContent";
import { ChevronIcon } from "../ui/Icons";
import { StatusBadge } from "../ui/StatusBadge";
import { ProposalCard } from "./ProposalCard";

type ArtifactProposalCardProps = {
  proposal: ArtifactProposal;
  busy: boolean;
  error: string | null;
  resolvedArtifact: ArtifactRecordSummary | null;
  onOpen?: () => void;
  onResolve: (decision: "accept" | "reject") => void;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

const charCount = (content: string) => content.length.toLocaleString("zh-CN");

const resolvedNoticeFor = (
  proposal: ArtifactProposal,
  resolvedArtifact: ArtifactRecordSummary | null,
): string => {
  if (proposal.status === "accepted") {
    const ordinal = resolvedArtifact?.currentVersionOrdinal;
    return `已保存到工作区 Artifact《${proposal.title}》${ordinal ? ` v${ordinal}` : ""}`;
  }
  if (proposal.status === "rejected") {
    return "已放弃该提案";
  }
  if (proposal.status === "cancelled") {
    return "提案已取消";
  }
  return "提案已处理";
};

type ArtifactReminderProps = ArtifactProposalCardProps & {
  expanded: boolean;
  onToggle: () => void;
};

/**
 * 待确认的 Artifact 提醒：折叠态只有一行（类型 / 标题 / 规模 / 操作），
 * 展开后才是完整正文（Markdown 渲染，独立滚动区域）。
 */
export function ArtifactReminder({
  proposal,
  busy,
  error,
  expanded,
  onToggle,
  onResolve,
}: ArtifactReminderProps) {
  const detailId = useId();
  const isUpdate = proposal.targetArtifactId !== null;
  const heading = isUpdate ? `更新《${proposal.title}》` : proposal.title;
  const meta = [
    isUpdate
      ? proposal.baseVersionOrdinal
        ? `基于 v${proposal.baseVersionOrdinal}`
        : "更新"
      : null,
    `${charCount(proposal.content)} 字`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <article
      aria-busy={busy}
      className={`proposal-card is-compact${busy ? " is-busy" : ""}${
        error ? " has-error" : ""
      }${expanded ? " is-expanded" : ""}`}
      data-state={busy ? "busy" : error ? "error" : "pending"}
    >
      <div className="proposal-compact-head">
        <span className="proposal-kind">{kindLabel[proposal.kind]}</span>
        <button
          aria-controls={detailId}
          aria-expanded={expanded}
          className="proposal-compact-toggle"
          onClick={onToggle}
          type="button"
        >
          <span className="proposal-compact-title">{heading}</span>
          <span className="proposal-compact-meta">{meta}</span>
          <ChevronIcon
            className="proposal-compact-chevron"
            direction={expanded ? "up" : "down"}
            size={14}
          />
        </button>
        <StatusBadge
          label={busy ? "处理中" : error ? "处理失败" : "待确认"}
          pulse={busy}
          tone={busy ? "active" : error ? "danger" : "warning"}
        />
        <div className="proposal-compact-actions">
          <button
            aria-label={isUpdate ? "更新文档" : "保留为 Artifact"}
            className="is-primary"
            disabled={busy}
            onClick={() => onResolve("accept")}
            title={isUpdate ? "更新文档" : "保留为 Artifact"}
            type="button"
          >
            {isUpdate ? "更新" : "保留"}
          </button>
          <button
            aria-label="放弃该提案"
            disabled={busy}
            onClick={() => onResolve("reject")}
            title="放弃该提案"
            type="button"
          >
            放弃
          </button>
        </div>
      </div>
      {expanded ? (
        <div className="proposal-compact-detail" id={detailId}>
          <p className="proposal-compact-reason">{proposal.reason}</p>
          <div className="proposal-compact-doc">
            {proposal.kind === "markdown" ? (
              <MessageContent content={proposal.content} />
            ) : (
              <pre className="proposal-compact-plain">{proposal.content}</pre>
            )}
          </div>
        </div>
      ) : null}
      {error ? (
        <div className="proposal-error" role="alert">
          {error}
        </div>
      ) : null}
    </article>
  );
}

export function ArtifactProposalCard(props: ArtifactProposalCardProps) {
  const [expanded, setExpanded] = useState(false);
  const { proposal, busy, error, resolvedArtifact, onOpen } = props;
  const isUpdate = proposal.targetArtifactId !== null;

  if (proposal.status !== "pending") {
    return (
      <ProposalCard
        busy={busy}
        error={error}
        kind={kindLabel[proposal.kind]}
        resolvedActions={
          proposal.status === "accepted" && onOpen ? (
            <button onClick={onOpen} type="button">
              打开 Artifact
            </button>
          ) : undefined
        }
        resolvedNotice={resolvedNoticeFor(proposal, resolvedArtifact)}
        status={proposal.status}
        title={isUpdate ? `更新《${proposal.title}》` : proposal.title}
      />
    );
  }

  return (
    <ArtifactReminder
      {...props}
      expanded={expanded}
      onToggle={() => setExpanded((current) => !current)}
    />
  );
}
