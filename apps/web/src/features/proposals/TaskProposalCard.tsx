import type { TaskProposal } from "../chat/apiTypes";
import { ProposalCard } from "./ProposalCard";

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
  const isReminder = proposal.schedule.kind === "once";
  const kindLabel = isReminder ? "提醒" : "已安排";

  let resolvedNotice = "提案已处理";
  if (proposal.status === "accepted")
    resolvedNotice = isReminder ? "提醒已记下，到时会自动执行" : "已安排，助手会按时做";
  else if (proposal.status === "rejected") resolvedNotice = "已忽略，不会安排";
  else if (proposal.status === "cancelled") resolvedNotice = "提案已取消";

  return (
    <ProposalCard
      kind={kindLabel}
      title={isReminder ? "Assistant 承诺到时做这件事" : "Assistant 承诺按时做这件事"}
      status={proposal.status}
      busy={busy}
      resolvedNotice={resolvedNotice}
      error={error}
      actions={
        <>
          <button disabled={busy} onClick={() => onResolve("accept")} type="button">
            {isReminder ? "提醒我" : "安排上"}
          </button>
          <button disabled={busy} onClick={() => onResolve("reject")} type="button">
            先不用
          </button>
        </>
      }
    >
      <p className="proposal-preview">{proposal.commitment}</p>
      <p className="proposal-reason">
        {proposal.scheduleDescription}
        {proposal.reason ? ` · ${proposal.reason}` : ""}
      </p>
    </ProposalCard>
  );
}
