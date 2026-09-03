import type { RuntimeV2Entry, RuntimeV2ProductEvent, RuntimeV2ToolState } from "./apiTypes";

/**
 * Per-tool execution display phase. Derived from the runtime snapshot and
 * rendered as a colored card. Internal states are collapsed into a small
 * user-facing vocabulary: "waiting" = paused on a checkpoint (approval),
 * "running" = actively being validated/executed, "pending" = queued.
 */
export type RuntimeToolPhase =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "rejected"
  | "cancelled";

export type RuntimeToolItem = {
  /** Stable React key: the tool execution id. */
  key: string;
  toolName: string;
  phase: RuntimeToolPhase;
  arguments?: unknown;
  result?: string;
  structuredContent?: unknown;
  errorCode?: string | null;
  /** Whether the tool execution carried an error (used to render a readable error). */
  isError: boolean;
  hasArgs: boolean;
  hasResult: boolean;
};

const toolStatePhase = (status: string): RuntimeToolPhase => {
  switch (status) {
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "rejected":
    case "expired":
      return "rejected";
    case "waiting_approval":
      return "waiting";
    case "created":
      return "pending";
    case "validating":
    case "running":
    default:
      return "running";
  }
};

export const toolStatusToPhase = toolStatePhase;

/** 时间线中的一项：一段助手文本 或 一次工具执行卡。 */
export type LiveTimelineItem =
  | { id: string; kind: "text"; text: string }
  | { id: string; kind: "tool"; tool: RuntimeToolItem };

const terminalRunTypes = new Set(["run.finished", "run.failed", "run.cancelled"]);

/**
 * 把某次 run 的运行时产品事件（message.updated 文本 delta 与 tool_execution.*
 * 工具事件，均按 eventSeq 保序）交错成一条时间线。文本 delta 累积成「文本片段」，
 * 工具事件在其发生位置插入工具卡，从而按 Agent 返回的实际时间流展示，
 * 而不是把整段文本放上面、把工具列表放下面。
 *
 * 工具卡的参数/结果/错误优先用快照里的 `tools`（按 toolExecutionId 对齐），
 * 事件里没有的字段（如 name/status）作为兜底。仅当能拿到该 run 的事件序列且
 * 确实出现工具时返回交错时间线，否则返回 null（调用方回退到现有布局）。
 */
export const buildRunTimeline = (
  events: RuntimeV2ProductEvent[] | undefined,
  tools: RuntimeToolItem[],
  runId: string | null | undefined,
): LiveTimelineItem[] | null => {
  if (!runId || !events?.length) return null;
  const runEvents = events.filter((event) => event.runId === runId);
  if (!runEvents.length) return null;
  const toolByExec = new Map<string, RuntimeToolItem>(
    tools.map((tool) => [tool.key, tool]),
  );

  const items: LiveTimelineItem[] = [];
  let text = "";
  const flush = (force: boolean) => {
    if (text.trim() || force) {
      items.push({ id: `text-${items.length}`, kind: "text", text });
      text = "";
    }
  };

  for (const event of runEvents) {
    if (event.type === "message.updated") {
      text += event.data.delta ?? "";
    } else if (
      event.type.startsWith("tool_execution.") &&
      event.data.toolExecutionId
    ) {
      flush(false);
      const execId = String(event.data.toolExecutionId);
      const existing = toolByExec.get(execId);
      const status = String(event.data.status ?? "running");
      items.push({
        id: `tool-${execId}`,
        kind: "tool",
        tool:
          existing ?? {
            key: execId,
            toolName: String(event.data.toolName ?? "工具"),
            phase: toolStatusToPhase(status),
            arguments: event.data.arguments,
            result: String(event.data.content ?? ""),
            structuredContent: undefined,
            errorCode: event.data.errorCode ?? null,
            isError:
              Boolean(event.data.errorCode) || status === "failed",
            hasArgs: event.data.arguments !== undefined,
            hasResult:
              Boolean(event.data.content) || Boolean(event.data.errorCode),
          },
      });
    } else if (terminalRunTypes.has(event.type)) {
      flush(false);
    }
  }
  flush(true);

  // 只有当时间线里确实出现了工具卡，且文本/工具并非纯打散块时才用交错布局。
  const hasTool = items.some((item) => item.kind === "tool");
  return hasTool ? items : null;
};

/**
 * Indexes tool_call / tool_result transcript entries by the model tool-call id
 * (`data.callId`). The backend emits `callId` on both the call and the result
 * payload, so linking is stable even when a call does not produce a matching
 * result (e.g. a malformed/parse-error call, a cancelled tool, or an approval
 * that never executed), unlike the former index-based alignment which drifted
 * whenever the call/result counts diverged.
 */
const indexToolEntries = (entries: RuntimeV2Entry[]) => {
  const calls = new Map<string, { toolName: string; arguments?: unknown }>();
  const results = new Map<
    string,
    { content: string; errorCode: string | null; structuredContent?: unknown }
  >();
  for (const entry of entries) {
    const callId = entry.data.callId;
    if (entry.type === "tool_call") {
      calls.set(callId ?? entry.id, {
        toolName: entry.data.toolName ?? "工具",
        arguments: entry.data.arguments,
      });
    } else if (entry.type === "tool_result") {
      results.set(callId ?? entry.id, {
        content: entry.data.content ?? "",
        errorCode: entry.data.errorCode ?? null,
        structuredContent: entry.data.structuredContent,
      });
    }
  }
  return { calls, results };
};

/**
 * Builds the ordered per-tool execution trace for the active run.
 *
 * `toolStates` is the authoritative per-execution status list (one card per
 * previously executed tool). Arguments come from `tool_call` entries and the
 * result from `tool_result` entries; both are matched by the model tool-call
 * id (`callId`). Mismatches degrade gracefully: a tool still renders with its
 * name + status even if args/result are absent (e.g. a tool that was approved
 * but not yet executed, or a call whose arguments were rejected at parse time).
 */
export const buildRuntimeToolTrace = (
  entries: RuntimeV2Entry[],
  toolStates: RuntimeV2ToolState[],
): RuntimeToolItem[] => {
  const { calls, results } = indexToolEntries(entries);
  return toolStates.map((tool, index) => {
    const call = tool.callId ? calls.get(tool.callId) : undefined;
    const result = tool.callId ? results.get(tool.callId) : undefined;
    const resultText = result?.content ?? "";
    const errorCode = result?.errorCode ?? null;
    return {
      key: tool.id ?? `tool-${index}`,
      toolName: tool.toolName || call?.toolName || "工具",
      phase: toolStatePhase(tool.status),
      arguments: call?.arguments,
      result: resultText,
      structuredContent: result?.structuredContent,
      errorCode,
      isError: Boolean(errorCode) || tool.status === "failed",
      hasArgs: call?.arguments !== undefined,
      hasResult: resultText !== "" || Boolean(errorCode),
    };
  });
};
