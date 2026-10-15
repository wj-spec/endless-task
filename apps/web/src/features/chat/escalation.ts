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
      elapsedSeconds: asNumber(progress.elapsedSeconds),
    },
    budget: {
      usedTokens: asNumber(budget.usedTokens),
      limitTokens:
        typeof budget.limitTokens === "number" ? budget.limitTokens : null,
      usedRatio:
        typeof budget.usedRatio === "number" ? budget.usedRatio : null,
      usedSeconds:
        typeof budget.usedSeconds === "number" ? budget.usedSeconds : null,
      limitSeconds:
        typeof budget.limitSeconds === "number" ? budget.limitSeconds : null,
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
  // 切片 10 A2（item 4）：**设置**方向也得按 runId 收紧（T4 只收紧了清空方向）。
  //
  // 作用域锚点与后端 `_escalation_json` 的 `running_run_id` 同源：快照里的
  // `runningRunId`/`activeRunId` 由 `runtimeController` 从 run 生命周期事件实时维护。
  // 但**已经有横幅时以横幅自己的 run 为准**——那才是这条横幅的"当前 run"：同会话里
  // 并发的侧车道/临时 run 发 `run.awaiting_user` 时，此前会无条件把它换成**别的**
  // run（用户正等着决策的那条横幅被顶掉），而刷新后快照（后端本来就按
  // `running_run_id` 过滤）又把它换回来——同一份事件流前后自相矛盾。
  // 锚点为空＝还没有任何作用域（例如快照还没到、本轮的第一条升级事件）：
  // 由第一条事件立起横幅；不这么做会把"实时升级"整条堵死。
  const anchorRunId = (): string | null =>
    current?.runId ?? snapshot?.runningRunId ?? snapshot?.activeRunId ?? null;
  for (const event of events ?? []) {
    const payload = (event.data ?? {}) as Record<string, unknown>;
    if (event.type === "run.awaiting_user") {
      const runId = asString(payload.runId, event.runId ?? "");
      const anchor = anchorRunId();
      if (anchor !== null && runId !== anchor) continue;
      current = escalationFromPayload(payload, event.runId ?? "");
      continue;
    }
    // 切片 10 整支终审修复 W1（item 2）：**别的** run 开始 → 这条作用域过期。
    // 与后端 `_awaiting_user_scope`（`gateway_impl/views.py`）同口径：硬停之后
    // 出现别的 run 的 `run.started` 时，快照不再返回这条升级——前端也得跟着放掉，
    // 否则"已安全停止、等你决定"会一直挂着对后续运行说谎。
    // 只对 `willStop=true` 生效：软阈值在 run 仍在飞时是活的，不归这条清理规则管。
    if (event.type === "run.started") {
      if (!current || !current.willStop) continue;
      const runId = asString(payload.runId, event.runId ?? "");
      if (runId !== current.runId) current = null;
      continue;
    }
    if (event.type === "run.progress_resumed" || ENDING_EVENT_TYPES.has(event.type)) {
      if (!current) continue;
      const runId = asString(payload.runId, event.runId ?? "");
      // 切片 10 T4（item 2）：终态事件和"进度恢复"一样必须**认领同一个 run**。
      // 事件窗口是每会话最近 200 条（`runtimeController`），且跨泳道混装：同会话里
      // 并发的侧车道/临时 run 一结束，此前会把正在等用户决策的主 run 横幅一起清掉。
      if (runId !== current.runId) continue;
      // 切片 10 整支终审修复 W1（item 1）：硬停（`willStop=true`）的
      // `run.awaiting_user` 后面**紧跟同一个 run** 的 `run.finished`——后端
      // `_escalation_json` 特意为终态的 `willStop=true` 保留投影（"老实停止必须
      // 可见"），前端在这里清掉就等于实时会话里看不见（SSE 不重放历史，只有刷新
      // 才从快照拿到那句提示）。软阈值（`willStop=false`）与 `stuck` 状态不受影响，
      // 仍旧"终态即清"（那是如实的：不再挂着）。
      if (current.willStop && ENDING_EVENT_TYPES.has(event.type)) continue;
      current = null;
    }
  }
  return current;
};

export const escalationReasonLabel = (reason: string): string => {
  if (reason === "budget_exhausted") return "上下文预算将尽";
  if (reason === "cost_cap_exceeded") return "成本达到上限";
  if (reason === "verification_failed") return "独立验证未通过";
  if (reason === "time_budget_exhausted") return "运行时长已到阈值";
  // 批次 E / E2（`uncertain_side_effects`）：中断时结果无法确认的工具副作用。
  // **必须**有这一条——未知 reason 会落到下面的兜底「连续无进展」，那是另一件事，
  // 对用户就是一句谎话（后端 escalation.py 的 `_REASONS` 已加入该值）。
  if (reason === "uncertain_side_effects") return "工具结果无法确认";
  return "连续无进展";
};

export const escalationHeadline = (state: RuntimeV2Escalation): string =>
  state.willStop
    ? `${escalationReasonLabel(state.reason)}：已安全停止，等你决定下一步`
    : `${escalationReasonLabel(state.reason)}：需要你决定下一步`;

/** "到目前为止完成了什么"。 */
export const escalationProgressText = (state: RuntimeV2Escalation): string => {
  const { modelTurns, toolCalls, toolFailures, producedCharacters, elapsedSeconds } =
    state.progress;
  const base = `已进行 ${modelTurns} 轮、工具调用 ${toolCalls} 次（失败 ${toolFailures} 次）、新增文本 ${producedCharacters} 字`;
  if (typeof elapsedSeconds !== "number" || elapsedSeconds <= 0) return base;
  const minutes = (elapsedSeconds / 60).toFixed(1);
  const limit = state.budget.limitSeconds;
  return typeof limit === "number" && limit > 0
    ? `${base}；已用时 ${minutes} 分钟（上限 ${Math.round(limit / 60)} 分钟）`
    : `${base}；已用时 ${minutes} 分钟`;
};

/**
 * 切片 10 A2（item 7）：硬停（`willStop=true`）的 run 上「继续」的**如实**说明。
 *
 * 硬停是"安全停止策略已经结束这次运行"（后端 `_awaiting_user_scope` 的注释：`run_awaiting_user(willStop=true)`
 * **紧跟** `run.finished`），所以它已经没有可注入指令的在飞 run——后端对终态 run 的
 * steer 如实回 `accepted:false`。按钮于是改成"新开一轮"：内容进输入框，明说没有向
 * 这个运行发任何指令。
 */
export const ESCALATION_FRESH_TURN_NOTE =
  "该运行已安全停止，无法继续；已把要追加的内容放进输入框，发送即开新一轮（没有向这个运行发任何指令）。";

/**
 * E3/B：**可复活**（`COMPLETED + awaiting_user`）的硬停 run 上点「继续」后的如实说明。
 *
 * 与上面那条（新开一轮）的分界是**后端判据** `snapshot.revivableRunId`：
 * 可复活 ⇒ 同一条 run 从断点接着跑（run id 不变、不新增用户消息）；
 * 不可复活（如 FAILED/CANCELLED、或该 lane 另有活动 run）⇒ 仍然新开一轮。
 * 如实说清两件事：① 没有新开一轮；② 停机时间不计入"已用"（复活是一段新的运行时间）。
 */
export const ESCALATION_RESUME_NOTE =
  "已在这条运行上接着跑（从上次停下的断点继续；没有新开一轮，停机时间不计入已用时长）。";

/**
 * 选项文案。"继续"在**已终态**（硬停）的 run 上有两种落点，按后端判据分流：
 * 可复活 ⇒ 「继续（接上这条运行）」；不可复活 ⇒ 「新开一轮」（C0 的权宜，仍是 FAILED
 * 这类形态的唯一出路）。运行仍在飞（`willStop=false`）时照旧是「继续」（steer）。
 */
export const escalationOptionLabel = (
  option: string,
  willStop = false,
  revivable = false,
): string => {
  if (option === ESCALATION_OPTION_CHANGE_APPROACH) return "换一条路径";
  if (option === ESCALATION_OPTION_TAKE_OVER) return "人工接管";
  if (!willStop) return "继续";
  return revivable ? "继续（接上这条运行）" : "新开一轮";
};

/** "继续"注入的指令：允许保留当前思路，但不重复已失败的调用。 */
export const escalationContinueMessage = (state: RuntimeV2Escalation): string =>
  state.repeatedFailures.length > 0
    ? "请继续推进，但不要重复已失败的调用；必要时换用其他方法。"
    : "请继续按当前思路推进。";
