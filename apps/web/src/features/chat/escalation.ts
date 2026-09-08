import type {
  RuntimeV2Escalation,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";

/**
 * C4 终止与升级：从快照 + 实时产品事件推导升级报告。
 *
 * 后端在无进展（连续同因失败）或上下文预算将尽时发 `run.awaiting_user`；
 * 用户处理完（进度恢复）或运行结束时清空。
 */
const ENDING_EVENT_TYPES = new Set([
  "run.finished",
  "run.failed",
  "run.cancelled",
]);

export const ESCALATION_OPTION_CONTINUE = "continue";
export const ESCALATION_OPTION_CHANGE_APPROACH = "change_approach";
export const ESCALATION_OPTION_TAKE_OVER = "take_over";

const asString = (value: unknown, fallback = ""): string =>
  typeof value === "string" && value.length > 0 ? value : fallback;

const asNumber = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

const asRecords = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          typeof item === "object" && item !== null,
      )
    : [];

export const escalationFromPayload = (
  payload: Record<string, unknown>,
  runId: string,
): RuntimeV2Escalation => {
  const progress =
    typeof payload.progress === "object" && payload.progress !== null
      ? (payload.progress as Record<string, unknown>)
      : {};
  const budget =
    typeof payload.budget === "object" && payload.budget !== null
      ? (payload.budget as Record<string, unknown>)
      : {};
  return {
    runId: asString(payload.runId, runId),
    reason: asString(payload.reason, "no_progress"),
    summary: asString(payload.summary),
    options: asStringArray(payload.options),
    progress: {
      modelTurns: asNumber(progress.modelTurns),
      toolCalls: asNumber(progress.toolCalls),
      toolFailures: asNumber(progress.toolFailures),
      producedCharacters: asNumber(progress.producedCharacters),
      inputTokens: asNumber(progress.inputTokens),
      outputTokens: asNumber(progress.outputTokens),
    },
    budget: {
      usedTokens: asNumber(budget.usedTokens),
      limitTokens:
        typeof budget.limitTokens === "number" ? budget.limitTokens : null,
      usedRatio:
        typeof budget.usedRatio === "number" ? budget.usedRatio : null,
    },
    repeatedFailures: asRecords(payload.repeatedFailures).map((item) => ({
      toolName: asString(item.toolName, "工具"),
      errorCode: asString(item.errorCode, "tool_error"),
      count: asNumber(item.count),
      safeMessage: asString(item.safeMessage),
    })),
    failures: asRecords(payload.failures).map((item) => ({
      toolName: asString(item.toolName, "工具"),
      errorCode: asString(item.errorCode, "tool_error"),
      safeMessage: asString(item.safeMessage),
      attempt: asNumber(item.attempt),
      toolExecutionId:
        typeof item.toolExecutionId === "string" ? item.toolExecutionId : null,
    })),
    guidance: asString(payload.guidance),
    willStop: payload.willStop === true,
    cost:
      typeof payload.cost === "object" && payload.cost !== null
        ? {
            usedUsd: asNumber((payload.cost as Record<string, unknown>).usedUsd),
            capUsd: asNumber((payload.cost as Record<string, unknown>).capUsd),
            priced: (payload.cost as Record<string, unknown>).priced === true,
          }
        : null,
    verdict:
      typeof payload.verdict === "object" && payload.verdict !== null
        ? {
            verdict: asString(
              (payload.verdict as Record<string, unknown>).verdict,
              "fail",
            ),
            reasons: asStringArray(
              (payload.verdict as Record<string, unknown>).reasons,
            ),
            missing: asStringArray(
              (payload.verdict as Record<string, unknown>).missing,
            ),
            model: asString(
              (payload.verdict as Record<string, unknown>).model,
            ),
          }
        : null,
  };
};

export const deriveEscalationState = (
  snapshot: RuntimeV2Snapshot | null | undefined,
  events: RuntimeV2ProductEvent[] | undefined,
): RuntimeV2Escalation | null => {
  let current = snapshot?.escalation ?? null;
  for (const event of events ?? []) {
    const payload = (event.data ?? {}) as Record<string, unknown>;
    if (event.type === "run.awaiting_user") {
      current = escalationFromPayload(payload, event.runId ?? "");
      continue;
    }
    if (event.type === "run.progress_resumed" || ENDING_EVENT_TYPES.has(event.type)) {
      if (!current) continue;
      const runId = asString(payload.runId, event.runId ?? "");
      if (event.type === "run.progress_resumed" && runId !== current.runId) {
        continue;
      }
      current = null;
    }
  }
  return current;
};

export const escalationReasonLabel = (reason: string): string => {
  if (reason === "budget_exhausted") return "上下文预算将尽";
  if (reason === "cost_cap_exceeded") return "成本达到上限";
  if (reason === "verification_failed") return "独立验证未通过";
  return "连续无进展";
};

export const escalationHeadline = (state: RuntimeV2Escalation): string =>
  state.willStop
    ? `${escalationReasonLabel(state.reason)}：已安全停止，等你决定下一步`
    : `${escalationReasonLabel(state.reason)}：需要你决定下一步`;

/** "到目前为止完成了什么"。 */
export const escalationProgressText = (state: RuntimeV2Escalation): string => {
  const { modelTurns, toolCalls, toolFailures, producedCharacters } =
    state.progress;
  return `已进行 ${modelTurns} 轮、工具调用 ${toolCalls} 次（失败 ${toolFailures} 次）、新增文本 ${producedCharacters} 字`;
};

export const escalationOptionLabel = (option: string): string => {
  if (option === ESCALATION_OPTION_CHANGE_APPROACH) return "换一条路径";
  if (option === ESCALATION_OPTION_TAKE_OVER) return "人工接管";
  return "继续";
};

/** "继续"注入的指令：允许保留当前思路，但不重复已失败的调用。 */
export const escalationContinueMessage = (state: RuntimeV2Escalation): string =>
  state.repeatedFailures.length > 0
    ? "请继续推进，但不要重复已失败的调用；必要时换用其他方法。"
    : "请继续按当前思路推进。";
