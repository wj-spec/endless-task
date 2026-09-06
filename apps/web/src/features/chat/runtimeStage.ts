import type { RuntimeV2ProductEvent } from "./apiTypes";
import type { PlanPayload } from "./PlanLine";

export type StageInput = {
  runId: string | null | undefined;
  /** 该轮 run 是否仍在活跃运行（created/queued/running/compacting…）。 */
  running: boolean;
  events?: RuntimeV2ProductEvent[];
  plan?: PlanPayload | null;
};

const stageForRunningTool = (
  events: RuntimeV2ProductEvent[],
  runId: string,
): string | null => {
  for (const event of [...events].reverse()) {
    if (
      event.runId !== runId ||
      !event.type.startsWith("tool_execution.") ||
      !event.data.toolExecutionId
    ) {
      continue;
    }
    const status = String(event.data.status ?? "");
    if (
      status === "created" ||
      status === "queued" ||
      status === "started" ||
      status === "running"
    ) {
      const toolName = event.data.toolName
        ? String(event.data.toolName)
        : "工具";
      return `正在执行「${toolName}」…`;
    }
  }
  return null;
};

const stageForCompaction = (
  events: RuntimeV2ProductEvent[],
  runId: string,
): string | null => {
  for (const event of [...events].reverse()) {
    if (event.runId !== runId) continue;
    if (event.type === "context.compaction_started") {
      return "正在整理上下文…";
    }
  }
  return null;
};

const stageForPlanStep = (plan: PlanPayload | null): string | null => {
  if (!plan) return null;
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const currentIndex =
    typeof plan.currentStepIndex === "number"
      ? plan.currentStepIndex
      : steps.findIndex((step) => step.status === "in_progress");
  const step = steps[currentIndex >= 0 ? currentIndex : -1];
  if (!step) return null;
  const title = typeof step.title === "string" ? step.title : "（未命名步骤）";
  return `推进「${title}」…`;
};

/**
 * S-A1：运行中最新轮轻量"阶段行"文案（S-E1 presenter 首批）。
 * 优先级：运行中工具 > 上下文整理 > 计划当前步；纯问答（无事实）→ null。
 */
export const runStageLabel = ({
  runId,
  running,
  events = [],
  plan,
}: StageInput): string | null => {
  if (!runId || !running) return null;
  return (
    stageForRunningTool(events, runId) ??
    stageForCompaction(events, runId) ??
    stageForPlanStep(plan ?? null)
  );
};
