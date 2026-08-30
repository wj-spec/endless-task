type StatusBadgeTone = "neutral" | "active" | "warning" | "danger" | "success";

type StatusBadgeProps = {
  label: string;
  tone?: StatusBadgeTone;
  pulse?: boolean;
};

export function StatusBadge({
  label,
  tone = "neutral",
  pulse = false,
}: StatusBadgeProps) {
  return (
    <span className={`status-badge is-${tone}${pulse ? " is-pulsing" : ""}`}>
      <span aria-hidden="true" className="status-badge-dot" />
      <span>{label}</span>
    </span>
  );
}