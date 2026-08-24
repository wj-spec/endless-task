import type { TaskProposal } from "../chat/apiTypes";

type TaskProposalCardProps = {
  proposal: TaskProposal;
  busy: boolean;
  error: string | null;
  onResolve: (decision: "accept" | "reject") => void;
};

export function TaskProposalCard({
  proposal,
  busy,
  error,
  onResolve,
}: TaskProposalCardProps) {
  if (proposal.status !== "pending") {
    let notice = "提案已处理";
    if (proposal.status === "accepted") notice = "已安排，助手会按时做";
    else if (proposal.status === "rejected") notice = "已忽略，不会安排";
    else if (proposal.status === "cancelled") notice = "提案已取消";
    return (
      <div className="proposal-card is-resolved">
        <span className="proposal-kind">已安排</span>
        <span>{notice}</span>
      </div>
    );
  }

  return (
    <div className="proposal-card">
      <div className="proposal-head">
        <span className="proposal-kind">已安排</span>
        <strong>Assistant 承诺按时做这件事</strong>
      </div>
      <p className="proposal-preview">{proposal.commitment}</p>
      <p className="proposal-reason">
        {proposal.scheduleDescription}
        {proposal.reason ? ` · ${proposal.reason}` : ""}
      </p>
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
          安排上
        </button>
        <button
          disabled={busy}
          onClick={() => onResolve("reject")}
          type="button"
        >
          先不用
        </button>
      </div>
    </div>
  );
}
