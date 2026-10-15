import type {
  RuntimeV2RunBudget,
  RuntimeV2RunState,
} from "./apiTypes";
import type { RuntimeConnectionPhase } from "./runtimeController";

/**
 * 切片 10 T4（item 1）：run 头部条的**纯推导**。
 *
 * 只消费切片 10 T2 后端真给的字段（`runState.startedAt` / `elapsedSeconds` /
 * `currentStep` / `budget`、`runningRunId`、快照 `usage`），以及既有的连接相位。
 * 后端没给的（例如"从未开始"的 run 的起点、没有 `run.budget_declared` 的墙）
 * 一律显示为"不知道"，**不编造**：不用"当前时间"当起点、不退回默认预算档。
 */

/** 与 `runtimeController.terminalRunStatuses` 同口径：终态之外都当成还在跑。 */
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);

/** 未知状态按"还在跑"处理：活时钟宁可多走一秒，也不要谎报"已停止"。 */
export const isRunActive = (status: string): boolean =>
  !TERMINAL_RUN_STATUSES.has(status);

const asNumberOrNull = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/**
 * 活时钟的秒数。三条来源，按"有多真"排序：
 *
 * 1. 运行中 + `startedAt`：`now - startedAt`（run 行上的真实起点，客户端自己连续跑，
 *    不必等事件刷新）；
 * 2. 终态：后端冻结的 `elapsedSeconds`（**不**继续走，停止后时钟必须停）；
 * 3. 运行中但后端没给起点（`startedAt` 为 null，例如刚 created 还没 start）：只能显示
 *    快照里的 `elapsedSeconds` 快照值，不拿"当前时间"当起点续跑。
 *
 * 三条都不成立（既无起点又无 elapsedSeconds）→ 返回 null，界面显示"—"。
 */
export const runElapsedSeconds = (
  runState: Pick<RuntimeV2RunState, "status" | "startedAt" | "elapsedSeconds">,
  nowMs: number,
): number | null => {
  const elapsed = asNumberOrNull(runState.elapsedSeconds);
  if (!isRunActive(runState.status)) return elapsed;
  const startedAt = runState.startedAt ? Date.parse(runState.startedAt) : NaN;
  if (Number.isFinite(startedAt)) {
    return Math.max(0, Math.floor((nowMs - startedAt) / 1000));
  }
  return elapsed;
};

/** 秒数区间文案（与既有 `DelegatedChildrenPanel.elapsedText` 同口径，不带前缀）。 */
export const formatElapsedCompact = (seconds: number | null): string => {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return `${whole} 秒`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) {
    const restSeconds = whole % 60;
    return restSeconds ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`;
};

export const RUN_STATUS_LABELS: Record<string, string> = {
  created: "已创建",
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等待审批",
  compacting: "整理上下文中",
  cancelling: "停止中",
  cancelled: "已停止",
  failed: "已失败",
  completed: "已完成",
};

export const runStatusLabel = (status: string): string =>
  RUN_STATUS_LABELS[status] ?? status;

/**
 * 当前工具/步骤文案。`currentStep` 为 null 表示**一个模型轮都还没有**（后端语义：
 * "还没开始干活"）→ 返回 null，由界面显示"尚未开始"，不猜一个工具出来。
 */
export const runStepText = (runState: RuntimeV2RunState): string | null => {
  const step = runState.currentStep;
  if (!step) return null;
  const turnCount = asNumberOrNull(step.modelTurnCount);
  const turn = turnCount !== null && turnCount > 0 ? `第 ${turnCount} 轮` : null;
  const tool =
    typeof step.toolName === "string" && step.toolName.length > 0
      ? step.toolName
      : null;
  const toolPart = tool
    ? step.toolInFlight
      ? `正在执行 ${tool}`
      : `最后一步 ${tool}`
    : null;
  const parts = [turn, toolPart].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : null;
};

/**
 * 预算文案。`budget` 为 null = 执行器没声明墙（后端**不**退回默认档）→ 返回 null，
 * 界面显示"—"，不编一个"大概 15 分钟"。
 */
export const runBudgetText = (budget: RuntimeV2RunBudget | null): string | null => {
  if (!budget) return null;
  const hard = asNumberOrNull(budget.hardSeconds);
  const remaining = asNumberOrNull(budget.remainingSeconds);
  const perTurn = asNumberOrNull(budget.perTurnSeconds);
  const costCap = asNumberOrNull(budget.costCapUsd);
  const parts: string[] = [];
  if (hard !== null && remaining !== null) {
    parts.push(`预算剩余 ${formatElapsedCompact(remaining)}（上限 ${formatElapsedCompact(hard)}）`);
  } else if (hard !== null) {
    parts.push(`预算上限 ${formatElapsedCompact(hard)}`);
  }
  if (perTurn !== null) parts.push(`单轮 ${formatElapsedCompact(perTurn)}`);
  if (costCap !== null) parts.push(`成本上限 $${costCap}`);
  return parts.length > 0 ? parts.join(" · ") : null;
};

/**
 * 连接相位文案。`reconnecting` 与既有 `RuntimeRecoveryNotices` 的重连卡片同一事实
 * （避免两处说法不一致）；`idle` = 没有在跟踪的运行，就是"已停止"。
 */
export const connectionPhaseLabel = (
  phase: RuntimeConnectionPhase | undefined,
): string => {
  if (phase === "connected") return "已连接";
  if (phase === "reconnecting") return "重连中";
  if (phase === "connecting") return "连接中";
  if (phase === "idle") return "已停止";
  return "未知";
};
