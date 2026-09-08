import type { AuditTrailEntry } from "./apiTypes";

/**
 * A8 决策解释/审计轨迹：面板上的可读文案。
 *
 * 理由/反事实/不确定性都由服务端从**运行事实**推导（不是模型复述思维链），
 * 这里只负责展示与分组。
 */

export const auditKindLabel = (kind: string): string => {
  const labels: Record<string, string> = {
    run: "运行",
    tool: "工具",
    approval: "审批",
    effect: "副作用",
    plan: "计划",
    memory: "记忆",
    verification: "验证",
    escalation: "升级",
    undo: "撤销",
  };
  return labels[kind] ?? "记录";
};

export const severityLabel = (severity: string): string => {
  if (severity === "critical") return "需要留意";
  if (severity === "warning") return "有影响";
  return "常规";
};

export const severityRank = (severity: string): number => {
  if (severity === "critical") return 0;
  if (severity === "warning") return 1;
  return 2;
};

/** 有"为什么"可展开的条目（其余只有标题/摘要）。 */
export const hasWhy = (entry: AuditTrailEntry): boolean =>
  Boolean(entry.rationale || entry.counterfactual || entry.uncertainty);

/** 默认收敛：先显示需要留意的，再按时间倒序。 */
export const orderAuditEntries = (
  entries: AuditTrailEntry[],
): AuditTrailEntry[] =>
  [...entries].sort((left, right) => {
    const rank = severityRank(left.severity) - severityRank(right.severity);
    if (rank !== 0) return rank;
    return right.occurredAt.localeCompare(left.occurredAt);
  });

export const auditSummaryLine = (counts: Record<string, number>): string => {
  const critical = counts.critical ?? 0;
  const warning = counts.warning ?? 0;
  const info = counts.info ?? 0;
  const parts = [`共 ${critical + warning + info} 条`];
  if (critical > 0) parts.push(`${critical} 条需要留意`);
  if (warning > 0) parts.push(`${warning} 条有影响`);
  return parts.join(" · ");
};

/** 选项键 → 中文（升级事件的可选项）。 */
export const auditOptionLabel = (option: string): string => {
  if (option === "continue") return "继续";
  if (option === "change_approach") return "换一条路径";
  if (option === "take_over") return "人工接管";
  return option;
};
