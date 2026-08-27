import type { KnowledgeProposal } from "../chat/apiTypes";

type KnowledgeProposalCardProps = {
  proposal: KnowledgeProposal;
  busy: boolean;
  error: string | null;
  onResolve: (decision: "accept" | "reject") => void;
};

export function KnowledgeProposalCard({
  proposal,
  busy,
  error,
  onResolve,
}: KnowledgeProposalCardProps) {
  const isAdd = proposal.type === "add_source";
  if (proposal.status !== "pending") {
    let notice = "提案已处理";
    if (proposal.status === "accepted") {
      notice = isAdd ? "已加入知识" : "已设为过期";
    } else if (proposal.status === "rejected") {
      notice = "已忽略，不会改动知识";
    } else if (proposal.status === "cancelled") {
      notice = "提案已取消";
    }
    return (
      <div className="proposal-card is-resolved">
        <span className="proposal-kind">知识</span>
        <span>{notice}</span>
      </div>
    );
  }

  return (
    <div className="proposal-card">
      <div className="proposal-head">
        <span className="proposal-kind">知识</span>
        <strong>{isAdd ? "Assistant 建议把这条信息加入知识" : "Assistant 建议把这条知识设为过期"}</strong>
      </div>
      {isAdd ? (
        <>
          <p className="proposal-preview">
            {proposal.payload.title ? `《${proposal.payload.title}》` : ""}
            {proposal.payload.content ?? ""}
          </p>
        </>
      ) : (
        <p className="proposal-preview">《{proposal.payload.title ?? proposal.payload.source_id}》</p>
      )}
      {proposal.payload.reason ? (
        <p className="proposal-reason">{proposal.payload.reason}</p>
      ) : null}
      {error ? (
        <div className="proposal-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="proposal-actions">
        <button disabled={busy} onClick={() => onResolve("accept")} type="button">
          {isAdd ? "加入知识" : "设为过期"}
        </button>
        <button disabled={busy} onClick={() => onResolve("reject")} type="button">
          不用
        </button>
      </div>
    </div>
  );
}
