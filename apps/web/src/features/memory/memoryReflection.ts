import type { MemoryReflectionRecord } from "../chat/apiTypes";

/**
 * B4 反思：记忆面板上的可读文案。
 *
 * 洞见是"从哪次失败里学到什么"，所以展示要同时给出**教训**与**来源**，
 * 让用户能判断它是否值得保留（确认/拒绝都走提案流）。
 */

export const reflectionTriggerLabel = (trigger: string): string => {
  if (trigger === "tool_failure") return "工具反复失败";
  if (trigger === "run_failure") return "运行失败";
  if (trigger === "escalation:no_progress") return "连续无进展";
  if (trigger === "escalation:verification_failed") return "独立验证未通过";
  if (trigger === "escalation:budget_exhausted") return "上下文预算将尽";
  if (trigger === "escalation:cost_cap_exceeded") return "成本达到上限";
  return "反思";
};

export const reflectionStatusLabel = (status: string): string => {
  if (status === "accepted") return "已记住";
  if (status === "rejected") return "已忽略";
  return "待确认";
};

/** 来源摘要：从哪次运行的哪些证据归纳出来的。 */
export const reflectionSourceText = (
  record: MemoryReflectionRecord,
): string => {
  const runId = record.runId ? record.runId.slice(-6) : "";
  const toolNames = new Set<string>();
  const errorCodes = new Set<string>();
  const collect = (item: Record<string, unknown>) => {
    if (typeof item.toolName === "string" && item.toolName) {
      toolNames.add(item.toolName);
    }
    if (typeof item.errorCode === "string" && item.errorCode) {
      errorCodes.add(item.errorCode);
    }
    const nested = item.refs;
    if (Array.isArray(nested)) {
      nested.forEach((entry) => {
        if (entry && typeof entry === "object") {
          collect(entry as Record<string, unknown>);
        }
      });
    }
  };
  record.sources.forEach(collect);
  const parts: string[] = [];
  if (runId) parts.push(`运行 …${runId}`);
  if (toolNames.size > 0) parts.push([...toolNames].join("、"));
  if (errorCodes.size > 0) parts.push([...errorCodes].join("、"));
  return parts.length > 0 ? `来源：${parts.join(" · ")}` : "来源：本次运行";
};
