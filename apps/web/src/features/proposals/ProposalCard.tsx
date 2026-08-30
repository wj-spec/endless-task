import type { ReactNode } from "react";
import { StatusBadge } from "../ui/StatusBadge";

type ProposalStatus = "pending" | "accepted" | "rejected" | "cancelled";

type ProposalCardProps = {
  kind: string;
  title?: ReactNode;
  status: ProposalStatus;
  busy: boolean;
  resolvedNotice: string;
  resolvedActions?: ReactNode;
  error?: string | null;
  children?: ReactNode;
  actions?: ReactNode;
};

const resolutionBadge = (status: Exclude<ProposalStatus, "pending">) => {
  if (status === "accepted") {
    return { label: "已采纳", tone: "success" as const };
  }
  if (status === "rejected") {
    return { label: "已忽略", tone: "neutral" as const };
  }
  return { label: "已取消", tone: "neutral" as const };
};

export function ProposalCard({
  kind,
  title,
  status,
  busy,
  resolvedNotice,
  resolvedActions,
  error,
  children,
  actions,
}: ProposalCardProps) {
  if (status !== "pending") {
    const badge = resolutionBadge(status);
    return (
      <article className="proposal-card is-resolved" data-state={status}>
        <div className="proposal-head">
          <span className="proposal-kind">{kind}</span>
          <StatusBadge label={badge.label} tone={badge.tone} />
        </div>
        <p className="proposal-resolution">{resolvedNotice}</p>
        {resolvedActions ? (
          <footer className="proposal-actions">{resolvedActions}</footer>
        ) : null}
      </article>
    );
  }

  return (
    <article
      aria-busy={busy}
      className={`proposal-card${busy ? " is-busy" : ""}${error ? " has-error" : ""}`}
      data-state={busy ? "busy" : error ? "error" : "pending"}
    >
      <header className="proposal-head">
        <span className="proposal-kind">{kind}</span>
        {title ? <strong>{title}</strong> : null}
        <StatusBadge
          label={busy ? "处理中" : error ? "处理失败" : "待确认"}
          pulse={busy}
          tone={busy ? "active" : error ? "danger" : "warning"}
        />
      </header>
      {children ? <div className="proposal-body">{children}</div> : null}
      {error ? (
        <div className="proposal-error" role="alert">
          {error}
        </div>
      ) : null}
      {actions ? <footer className="proposal-actions">{actions}</footer> : null}
    </article>
  );
}
