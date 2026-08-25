import type { ReactNode } from "react";

type EmptyStateProps = {
  symbol?: string;
  title: string;
  desc?: string;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({
  symbol = "∞",
  title,
  desc,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={className ? `empty-state ${className}` : "empty-state"}>
      <span aria-hidden="true" className="empty-symbol">
        {symbol}
      </span>
      <h3>{title}</h3>
      {desc ? <p>{desc}</p> : null}
      {action ?? null}
    </div>
  );
}
