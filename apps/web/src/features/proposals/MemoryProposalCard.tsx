import type { MemoryProposal } from "../chat/apiTypes";
import { ProposalCard } from "./ProposalCard";

type MemoryProposalCardProps = {
  proposal: MemoryProposal;
  busy: boolean;
  error: string | null;
  onResolve: (decision: "accept" | "reject") => void;
};

export function MemoryProposalCard({
  proposal,
  busy,
  error,
  onResolve,
}: MemoryProposalCardProps) {
  let resolvedNotice = "提案已处理";
  if (proposal.status === "accepted") resolvedNotice = "已记住这条信息";
  else if (proposal.status === "rejected") resolvedNotice = "已忽略，不会记住";
  else if (proposal.status === "cancelled") resolvedNotice = "提案已取消";

  return (
    <ProposalCard
      kind="记忆"
      title="Assistant 想记住这条信息"
      status={proposal.status}
      busy={busy}
      resolvedNotice={resolvedNotice}
      error={error}
      actions={
        <>
          <button disabled={busy} onClick={() => onResolve("accept")} type="button">
            记住
          </button>
          <button disabled={busy} onClick={() => onResolve("reject")} type="button">
            不记
          </button>
        </>
      }
    >
      <p className="proposal-preview">{proposal.content}</p>
      {proposal.reason ? <p className="proposal-reason">{proposal.reason}</p> : null}
    </ProposalCard>
  );
}
