import type { RuntimeV2UsageSummary } from "./apiTypes";

/**
 * C5 成本/延迟可见：把用量/成本/耗时压成一行可读文案。
 *
 * 成本是**估算**（后端按定价表计算），未定价模型不显示假数字。
 */

export const formatTokens = (tokens: number): string => {
  if (!Number.isFinite(tokens) || tokens < 0) return "0";
  if (tokens >= 1_000_000) {
    const millions = tokens / 1_000_000;
    return `${millions >= 10 ? Math.round(millions) : millions.toFixed(1)}M`;
  }
  if (tokens >= 1_000) {
    const thousands = tokens / 1_000;
    return `${thousands >= 10 ? Math.round(thousands) : thousands.toFixed(1)}K`;
  }
  return String(Math.round(tokens));
};

export const formatCost = (costUsd: number | null, priced: boolean): string => {
  if (costUsd === null || !priced) return "未定价";
  if (costUsd < 1) return `~$${costUsd.toFixed(4)}`;
  return `~$${costUsd.toFixed(2)}`;
};

export const formatDuration = (durationMs: number | null): string => {
  if (durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) {
    return "—";
  }
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  const seconds = durationMs / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds - minutes * 60)}s`;
};

/** 一行用量摘要：本轮 1.2K in / 480 out · ~$0.0040 · 8.3s。 */
export const usageLine = (usage: RuntimeV2UsageSummary): string => {
  const parts = [
    `${formatTokens(usage.inputTokens)} in / ${formatTokens(usage.outputTokens)} out`,
    formatCost(usage.costUsd, usage.costPriced),
    formatDuration(usage.durationMs),
  ];
  if (usage.firstTokenLatencyMs !== null) {
    parts.push(`首字 ${formatDuration(usage.firstTokenLatencyMs)}`);
  }
  return parts.join(" · ");
};

/** 定价口径说明（成本是估算，避免误导）。 */
export const usageTooltip = (usage: RuntimeV2UsageSummary): string => {
  if (!usage.costPriced) {
    return "成本为估算：当前模型未在定价表中，只显示 token 用量。";
  }
  const revision = usage.priceRevision ? `（定价表 ${usage.priceRevision}）` : "";
  const partial =
    usage.unpricedTurns > 0
      ? `；其中 ${usage.unpricedTurns} 轮未定价，实际成本可能更高`
      : "";
  return `成本为估算，按 token 与定价表计算${revision}${partial}。`;
};
