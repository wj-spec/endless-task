import type {
  ResponseVariantSnapshot,
  RuntimeV2Snapshot,
  TurnStatus,
} from "./apiTypes";
import type { PlanPayload } from "./PlanLine";

/**
 * 轮次展示层的纯函数与常量（从 `ChatWorkSurface.tsx` 抽出，行为零改动）。
 *
 * 为什么单独成文件：这些函数被轮次渲染逻辑与工作面本身共用，且没有任何 JSX/组件依赖。
 * 抽成叶子模块后，下一步把轮次渲染块拆成 `TurnItem` 时可以直接 import，而不会形成
 * `ChatWorkSurface → TurnItem → ChatWorkSurface` 的循环依赖。
 *
 * 同时补上了此前完全没有单测的两条产品规则：审批风险的兜底推导（后端没下发 risk 时
 * 按工具名判高危）与"取最后一版计划"。
 */
export type TurnStatusPresentation = {
  label: string;
  tone: "neutral" | "active" | "warning" | "danger";
  pulse: boolean;
};

export const turnStatusPresentation = (
  status: TurnStatus,
  waitingApproval: boolean,
): TurnStatusPresentation | null => {
  if (waitingApproval) {
    return { label: "等待确认", tone: "warning", pulse: false };
  }
  if (status === "created") {
    return { label: "准备回答", tone: "active", pulse: true };
  }
  if (status === "running") {
    return { label: "正在回答", tone: "active", pulse: true };
  }
  if (status === "failed") {
    return { label: "回答失败", tone: "danger", pulse: false };
  }
  if (status === "cancelled") {
    return { label: "已停止", tone: "neutral", pulse: false };
  }
  return null;
};


export const APPROVAL_RISK_LABELS: Record<"low" | "medium" | "high", string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
};

// 审批风险等级：优先用后端下发的 risk；缺失时按工具名兜底推导。
export const approvalRisk = (
  metadata?: Record<string, unknown>,
): "low" | "medium" | "high" => {
  const risk = metadata?.risk;
  if (risk === "low" || risk === "medium" || risk === "high") return risk;
  const name = String(metadata?.toolName ?? "").toLowerCase();
  if (/(delete|remove|drop|truncate|wipe|unlink)/.test(name)) return "high";
  return "medium";
};

export const latestPlanForRun = (
  runtimeSnapshot: RuntimeV2Snapshot | null | undefined,
  runId: string,
): PlanPayload | null => {  if (!runtimeSnapshot || !runId) return null;
  const entries = runtimeSnapshot.entries ?? [];
  const planEntries = entries.filter(
    (entry) =>
      entry.type === "plan" && entry.sourceRunId === runId,
  );
  if (planEntries.length === 0) return null;
  const last = planEntries[planEntries.length - 1];
  const reference = last.data.reference;
  if (typeof reference !== "object" || reference === null) return null;
  const payload = reference as Record<string, unknown>;
  return {
    title: payload.title,
    steps: Array.isArray(payload.steps)
      ? (payload.steps as PlanPayload["steps"])
      : undefined,
    currentStepIndex: payload.currentStepIndex,
  };
};

export function findActiveVariant(
  variants: ResponseVariantSnapshot[],
  activeId: string | null,
) {
  return variants.find((item) => item.variant.id === activeId) ?? variants.at(-1);
}
