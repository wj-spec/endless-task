import type { RuntimeV2Snapshot } from "./apiTypes";

type ContextBudgetMeterProps = {
  budget: RuntimeV2Snapshot["contextBudget"];
};

const WARN_AT = 0.8;
const HIGH_AT = 0.95;

export function ContextBudgetMeter({ budget }: ContextBudgetMeterProps) {
  if (!budget) return null;
  const ratio = budget.usedRatio ?? 0;
  const percent = Math.round(ratio * 100);
  const tone = ratio >= HIGH_AT ? "high" : ratio >= WARN_AT ? "warn" : "ok";
  return (
    <div className={`context-budget is-${tone}`} role="status">
      <span className="context-budget-label">上下文 {percent}%</span>
      <span className="context-budget-bar" aria-hidden="true">
        <span className="context-budget-fill" style={{ width: `${percent}%` }} />
      </span>
      {tone !== "ok" ? (
        <span className="context-budget-hint">
          {tone === "high" ? "上下文已满，建议新会话" : "上下文接近上限"}
        </span>
      ) : null}
    </div>
  );
}
