import type { RuntimeV2Snapshot } from "./apiTypes";

type ContextBudgetMeterProps = {
  budget: RuntimeV2Snapshot["contextBudget"];
};

const WARN_AT = 0.8;
const HIGH_AT = 0.95;
/** 环形直径（px）：比发送按钮略小，作为输入区左下角的常驻状态灯。 */
const SIZE = 26;
const STROKE = 3;
const RADIUS = (SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

const formatTokens = (tokens: number): string => {
  if (!Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return String(Math.round(tokens));
};

/**
 * A2：上下文预算指示（环形，输入框左下角常驻）。
 *
 * 显示的是**当前占用**（最近一轮请求的上下文大小），不是本轮累计消耗——累计值
 * 放在 tooltip 里作参考，避免几轮工具调用就把环推满造成误导。风格沿用产品既有
 * 的细描边 + 语义色（accent / warn / danger）。
 */
export function ContextBudgetMeter({ budget }: ContextBudgetMeterProps) {
  if (!budget) return null;
  const ratio = Math.min(1, Math.max(0, budget.usedRatio ?? 0));
  const percent = Math.round(ratio * 100);
  const tone = ratio >= HIGH_AT ? "high" : ratio >= WARN_AT ? "warn" : "ok";
  const detail = [
    `上下文 ${percent}%`,
    `当前 ${formatTokens(budget.usedTokens)} / ${formatTokens(budget.limitTokens)}`,
    `剩余 ${formatTokens(budget.remainingTokens)}`,
  ];
  if (
    typeof budget.cumulativeTokens === "number" &&
    budget.cumulativeTokens > budget.usedTokens
  ) {
    detail.push(`本轮累计 ${formatTokens(budget.cumulativeTokens)}`);
  }
  const title = detail.join(" · ");

  return (
    <span
      aria-label={title}
      className={`context-ring is-${tone}`}
      role="img"
      title={title}
    >
      <svg
        aria-hidden="true"
        height={SIZE}
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        width={SIZE}
      >
        <circle
          className="context-ring-track"
          cx={SIZE / 2}
          cy={SIZE / 2}
          fill="none"
          r={RADIUS}
          strokeWidth={STROKE}
        />
        <circle
          className="context-ring-fill"
          cx={SIZE / 2}
          cy={SIZE / 2}
          fill="none"
          r={RADIUS}
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={CIRCUMFERENCE * (1 - ratio)}
          strokeLinecap="round"
          strokeWidth={STROKE}
          transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
        />
      </svg>
      <span className="context-ring-value">{percent}</span>
      {tone === "high" ? (
        <span className="context-ring-hint">将满</span>
      ) : null}
    </span>
  );
}
