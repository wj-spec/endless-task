import type { ArtifactProposal, ArtifactRecordSummary } from "../chat/apiTypes";
import { ProposalCard } from "./ProposalCard";

const PREVIEW_CHARS = 200;

type ArtifactProposalCardProps = {
  proposal: ArtifactProposal;
  busy: boolean;
  error: string | null;
  resolvedArtifact: ArtifactRecordSummary | null;
  onResolve: (decision: "accept" | "reject") => void;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

export function ArtifactProposalCard({
  proposal,
  busy,
  error,
  resolvedArtifact,
  onResolve,
}: ArtifactProposalCardProps) {
  const isUpdate = proposal.targetArtifactId !== null;

  let resolvedNotice = "提案已处理";
  if (proposal.status === "accepted") {
    const ordinal = resolvedArtifact?.currentVersionOrdinal;
    resolvedNotice = `已保存为《${proposal.title}》${ordinal ? ` v${ordinal}` : ""}`;
  } else if (proposal.status === "rejected") {
    resolvedNotice = "已放弃该提案";
  } else if (proposal.status === "cancelled") {
    resolvedNotice = "提案已取消";
  }

  const preview =
    proposal.content.length > PREVIEW_CHARS
      ? `${proposal.content.slice(0, PREVIEW_CHARS)}…`
      : proposal.content;
  const heading = isUpdate
    ? `更新《${proposal.title}》${
        proposal.baseVersionOrdinal ? `（基于 v${proposal.baseVersionOrdinal}）` : ""
      }`
    : proposal.title;

  return (
    <ProposalCard
      kind={kindLabel[proposal.kind]}
      title={heading}
      status={proposal.status}
      busy={busy}
      resolvedNotice={resolvedNotice}
      error={error}
      actions={
        <>
          <button disabled={busy} onClick={() => onResolve("accept")} type="button">
            {isUpdate ? "更新文档" : "保留为 Artifact"}
          </button>
          <button disabled={busy} onClick={() => onResolve("reject")} type="button">
            放弃
          </button>
        </>
      }
    >
      <p className="proposal-reason">{proposal.reason}</p>
      <p className="proposal-preview">{preview}</p>
      {proposal.content.length > PREVIEW_CHARS ? (
        <details className="proposal-full">
          <summary>展开完整内容</summary>
          <div className="proposal-full-body">{proposal.content}</div>
        </details>
      ) : null}
    </ProposalCard>
  );
}
