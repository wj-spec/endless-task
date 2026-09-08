import type { MemoryConsolidationRecord } from "../chat/apiTypes";

/**
 * B2 记忆巩固：面板上的可读文案。
 *
 * 巩固不改写用户记忆，而是"提一条合并后的高层记忆 → 用户确认 → 原记忆标记已并入"。
 * 所以这里的状态文案要能说清"现在处于哪一步、原记忆去哪了"。
 */

export const consolidationStatusLabel = (status: string): string => {
  if (status === "accepted") return "已并入";
  if (status === "rejected") return "已放弃";
  return "待确认";
};

export const consolidationSummary = (
  record: MemoryConsolidationRecord,
): string => {
  const count = record.sources.length;
  const status = consolidationStatusLabel(record.status);
  return `合并 ${count} 条同类记忆 · ${status}`;
};

/** 原记忆的"已并入"说明：告诉用户它为什么不再单独出现。 */
export const mergedIntoText = (record: MemoryConsolidationRecord): string =>
  record.insightMemoryId
    ? "已并入一条更高层的记忆（可在上方找到）"
    : "已并入一条更高层的记忆";

export const consolidationSourcePreview = (
  record: MemoryConsolidationRecord,
  limit = 3,
): string[] => record.sources.slice(0, limit).map((source) => source.content);
