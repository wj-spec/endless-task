import type { MemoryRecord } from "../chat/apiTypes";

/**
 * B3 重要性加权遗忘：记忆面板用的可读文案与档位。
 *
 * 重要性与"该不该留"的关系是单调的：重要/常被用到 → 更该留；
 * 久未用到 → 更容易被忘。这里只做展示，判定逻辑在服务端。
 */

export const IMPORTANCE_LEVELS: ReadonlyArray<{
  value: number;
  label: string;
}> = [
  { value: 0.2, label: "不重要" },
  { value: 0.5, label: "普通" },
  { value: 0.8, label: "重要" },
  { value: 1.0, label: "关键" },
];

/** 把 0~1 的重要性映射成档位文案（就近取档）。 */
export const importanceLabel = (importance: number): string => {
  const value = Number.isFinite(importance) ? importance : 0.5;
  let best: { value: number; label: string } = IMPORTANCE_LEVELS[1];
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const level of IMPORTANCE_LEVELS) {
    const distance = Math.abs(level.value - value);
    if (distance < bestDistance) {
      best = level;
      bestDistance = distance;
    }
  }
  return best.label;
};

export const isImportant = (importance: number): boolean =>
  (Number.isFinite(importance) ? importance : 0) >= 0.7;

/** 记忆"该不该留"的一句话依据：访问次数 + 最近使用。 */
export const retentionHint = (
  memory: MemoryRecord,
  now: number = Date.now(),
): string => {
  const parts: string[] = [];
  if (memory.pinned) parts.push("已钉住");
  if (memory.accessCount > 0) {
    parts.push(`被用到 ${memory.accessCount} 次`);
  } else {
    parts.push("还没被用到过");
  }
  if (memory.lastAccessedAt) {
    parts.push(`最近 ${formatDaysSince(memory.lastAccessedAt, now)}`);
  }
  return parts.join(" · ");
};

export const formatDaysSince = (
  timestamp: string,
  now: number = Date.now(),
): string => {
  const parsed = Date.parse(timestamp);
  if (Number.isNaN(parsed)) return "时间未知";
  const days = Math.floor((now - parsed) / 86_400_000);
  if (days <= 0) return "今天用过";
  if (days === 1) return "昨天用过";
  if (days < 30) return `${days} 天前用过`;
  const months = Math.floor(days / 30);
  return `${months} 个月前用过`;
};

/** 遗忘概率 → 可读风险文案。 */
export const forgetRiskLabel = (probability: number): string => {
  if (!Number.isFinite(probability)) return "未知";
  if (probability >= 0.8) return "很可能被忘";
  if (probability >= 0.5) return "可能被忘";
  if (probability >= 0.2) return "暂时安全";
  return "很难被忘";
};
