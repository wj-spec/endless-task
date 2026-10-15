import type {
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeV2StuckState,
} from "./apiTypes";

/**
 * C2 失败记忆：从快照 + 实时产品事件推导"卡住"状态。
 *
 * 后端把失败记忆作为外部状态落成 `run.stuck` / `run.progress_resumed`
 * 事件；前端只需按时间折叠这两个事件，并在运行结束时清空。
 */
const ENDING_EVENT_TYPES = new Set([
  "run.finished",
  "run.failed",
  "run.cancelled",
]);

const asString = (value: unknown, fallback = ""): string =>
  typeof value === "string" && value.length > 0 ? value : fallback;

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

const asRepeatedFailures = (
  value: unknown,
): RuntimeV2StuckState["repeatedFailures"] =>
  Array.isArray(value)
    ? value
        .filter(
          (item): item is Record<string, unknown> =>
            typeof item === "object" && item !== null,
        )
        .map((item) => ({
          toolName: asString(item.toolName, "工具"),
          errorCode: asString(item.errorCode, "tool_error"),
          count: typeof item.count === "number" ? item.count : 0,
          safeMessage: asString(item.safeMessage),
        }))
    : [];

const asAttempts = (value: unknown): RuntimeV2StuckState["attempts"] =>
  Array.isArray(value)
    ? value
        .filter(
          (item): item is Record<string, unknown> =>
            typeof item === "object" && item !== null,
        )
        .map((item) => ({
          toolName: asString(item.toolName, "工具"),
          errorCode: asString(item.errorCode, "tool_error"),
          safeMessage: asString(item.safeMessage),
          attempt: typeof item.attempt === "number" ? item.attempt : 0,
          retryable: item.retryable === true,
          toolExecutionId:
            typeof item.toolExecutionId === "string"
              ? item.toolExecutionId
              : null,
        }))
    : [];

export const stuckStateFromPayload = (
  payload: Record<string, unknown>,
  runId: string,
): RuntimeV2StuckState => ({
  runId: asString(payload.runId, runId),
  level: asString(payload.level, "remind"),
  detector: asString(payload.detector),
  reasons: asStringArray(payload.reasons),
  consecutive:
    typeof payload.consecutive === "number" ? payload.consecutive : null,
  repeatedFailures: asRepeatedFailures(payload.repeatedFailures),
  attempts: asAttempts(payload.attempts),
  guidance: asString(payload.guidance),
});

export const deriveStuckState = (
  snapshot: RuntimeV2Snapshot | null | undefined,
  events: RuntimeV2ProductEvent[] | undefined,
): RuntimeV2StuckState | null => {
  let current = snapshot?.stuck ?? null;
  // 切片 10 A2（item 4）：**设置**方向也得按 runId 收紧（T4 只收紧了清空方向）。
  //
  // 锚点口径与后端 `_stuck_json` 的 `running_run_id` 同源（快照的
  // `runningRunId`/`activeRunId` 由 `runtimeController` 从 run 生命周期事件实时维护），
  // 但**已有卡住横幅时以它自己的 run 为准**：同会话并发的侧车道/临时 run 发
  // `run.stuck` 时，此前会无条件把当前 run 的横幅换成**别的** run。锚点为空
  // （还没有任何作用域）时由第一条事件立起横幅。
  const anchorRunId = (): string | null =>
    current?.runId ?? snapshot?.runningRunId ?? snapshot?.activeRunId ?? null;
  for (const event of events ?? []) {
    const payload = (event.data ?? {}) as Record<string, unknown>;
    if (event.type === "run.stuck") {
      const runId = asString(payload.runId, event.runId ?? "");
      const anchor = anchorRunId();
      if (anchor !== null && runId !== anchor) continue;
      current = stuckStateFromPayload(payload, event.runId ?? "");
      continue;
    }
    if (event.type === "run.progress_resumed" || ENDING_EVENT_TYPES.has(event.type)) {
      if (!current) continue;
      const runId = asString(payload.runId, event.runId ?? "");
      // 切片 10 T4（item 2）：终态事件和"进度恢复"一样必须**认领同一个 run**。
      // 事件窗口是每会话最近 200 条（`runtimeController`），且跨泳道混装：同会话里
      // 并发的侧车道/临时 run 一结束，此前会把当前 run 的卡住横幅一起清掉。
      if (runId !== current.runId) continue;
      current = null;
    }
  }
  return current;
};

/** 卡住标题：级别越高，措辞越强。 */
export const stuckHeadline = (state: RuntimeV2StuckState): string => {
  if (state.level === "stop") return "已安全停止：连续无进展";
  if (state.level === "restrict") return "连续失败，建议换一条路径";
  return "这一组操作反复失败，可能卡住了";
};

/** 一句话失败摘要，用于列表主行。 */
export const repeatedFailureText = (
  item: RuntimeV2StuckState["repeatedFailures"][number],
): string =>
  `${item.toolName} · ${item.errorCode} · 连续 ${item.count} 次`;

/** "换路径"要注入的纠偏指令（复用运行中 steer 通道；C4 升级视图同样复用）。 */
export const stuckSteerMessage = (state: {
  repeatedFailures: RuntimeV2StuckState["repeatedFailures"];
}): string => {
  const failures = state.repeatedFailures
    .map((item) => `${item.toolName}(${item.errorCode}×${item.count})`)
    .join("、");
  const detail = failures ? `已失败：${failures}。` : "";
  return `${detail}请换一种方法或参数继续，不要原样重复同一个调用。`;
};
