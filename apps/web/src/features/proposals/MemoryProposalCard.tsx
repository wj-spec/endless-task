import type { MemoryProposal } from "../chat/apiTypes";

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
  if (proposal.status !== "pending") {
    let notice = "提案已处理";
    if (proposal.status === "accepted") notice = "已记住这条信息";
    else if (proposal.status === "rejected") notice = "已忽略，不会记住";
    else if (proposal.status === "cancelled") notice = "提案已取消";
    return (
      <div className="proposal-card is-resolved">
        <span className="proposal-kind">记忆</span>
        <span>{notice}</span>
      </div>
    );
  }

  return (
    <div className="proposal-card">
      <div className="proposal-head">
        <span className="proposal-kind">记忆</span>
        <strong>Assistant 想记住这条信息</strong>
      </div>
      <p className="proposal-preview">{proposal.content}</p>
      {proposal.reason ? <p className="proposal-reason">{proposal.reason}</p> : null}
      {error ? (
        <div className="proposal-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="proposal-actions">
        <button
          disabled={busy}
          onClick={() => onResolve("accept")}
          type="button"
        >
          记住
        </button>
        <button
          disabled={busy}
          onClick={() => onResolve("reject")}
          type="button"
        >
          不记
        </button>
      </div>
    </div>
  );
}
