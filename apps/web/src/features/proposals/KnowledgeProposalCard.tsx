import { useState } from "react";
import type { KnowledgeProposal, Workspace } from "../chat/apiTypes";

type KnowledgeProposalCardProps = {
  proposal: KnowledgeProposal;
  busy: boolean;
  error: string | null;
  conversationWorkspaceId?: string | null;
  workspaces?: Workspace[];
  onResolve: (decision: "accept" | "reject", workspaceId?: string | null) => void;
};

export function KnowledgeProposalCard({
  proposal,
  busy,
  error,
  conversationWorkspaceId = null,
  workspaces = [],
  onResolve,
}: KnowledgeProposalCardProps) {
  const isAdd = proposal.type === "add_source";
  const isMerge = proposal.type === "merge_source";
  const isDecay = !isAdd && !isMerge && Boolean(proposal.payload.decay);
  const proposedGlobal = proposal.payload.workspace_id == null;
  const [scopeChoice, setScopeChoice] = useState<"global" | "workspace">(
    proposedGlobal ? "global" : "workspace",
  );
  const canSwitchScope =
    isAdd && proposal.status === "pending" && conversationWorkspaceId !== null;
  const workspaceTargetId =
    proposal.payload.workspace_id ?? conversationWorkspaceId;
  const workspaceName =
    workspaces.find((item) => item.id === workspaceTargetId)?.name ?? "工作区";

  if (proposal.status !== "pending") {
    let notice = "提案已处理";
    if (proposal.status === "accepted") {
      notice = isAdd ? "已加入知识" : isMerge ? "已过期重复的一条" : "已设为过期";
    } else if (proposal.status === "rejected") {
      notice = isMerge ? "已忽略，两条都保留" : "已忽略，不会改动知识";
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

  let heading = "Assistant 建议把这条信息加入知识";
  if (isMerge) {
    heading = "Assistant 发现这条知识与已有知识重复";
  } else if (!isAdd) {
    heading = isDecay
      ? "Assistant 想确认这条知识是否仍然有效"
      : "Assistant 建议把这条知识设为过期";
  }
  const acceptLabel = isAdd
    ? "加入知识"
    : isMerge
      ? "过期重复的一条"
      : isDecay
        ? "让它过期"
        : "设为过期";
  const rejectLabel = isMerge ? "都保留" : "不用";

  const acceptWorkspaceId =
    scopeChoice === "global" ? null : workspaceTargetId;

  return (
    <div className="proposal-card">
      <div className="proposal-head">
        <span className="proposal-kind">知识</span>
        <strong>{heading}</strong>
      </div>
      {isAdd ? (
        <p className="proposal-preview">
          {proposal.payload.title ? `《${proposal.payload.title}》` : ""}
          {proposal.payload.content ?? ""}
        </p>
      ) : isMerge ? (
        <p className="proposal-preview">
          《{proposal.payload.title ?? proposal.payload.source_id}》与
          《{proposal.payload.target_title ?? proposal.payload.target_id}》
          {typeof proposal.payload.overlap === "number"
            ? `，相似度 ${Math.round(proposal.payload.overlap * 100)}%`
            : ""}
        </p>
      ) : (
        <p className="proposal-preview">
          《{proposal.payload.title ?? proposal.payload.source_id}》
        </p>
      )}
      {isAdd ? (
        <p className="proposal-scope">
          将存入：
          {scopeChoice === "global" ? "全局知识" : `工作区《${workspaceName}》`}
          {canSwitchScope ? (
            <button
              disabled={busy}
              onClick={() =>
                setScopeChoice(scopeChoice === "global" ? "workspace" : "global")
              }
              type="button"
            >
              {scopeChoice === "global" ? "改存工作区" : "改存全局"}
            </button>
          ) : null}
        </p>
      ) : null}
      {proposal.payload.reason ? (
        <p className="proposal-reason">{proposal.payload.reason}</p>
      ) : null}
      {error ? (
        <div className="proposal-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="proposal-actions">
        <button
          disabled={busy}
          onClick={() =>
            onResolve("accept", isAdd ? acceptWorkspaceId : undefined)
          }
          type="button"
        >
          {acceptLabel}
        </button>
        <button disabled={busy} onClick={() => onResolve("reject")} type="button">
          {rejectLabel}
        </button>
      </div>
    </div>
  );
}
