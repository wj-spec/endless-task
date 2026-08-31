import type { RuntimeV2Entry, RuntimeV2ToolState } from "./apiTypes";

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
  errorCode?: string | null;
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

/**
 * Builds the ordered per-tool execution trace for the active run.
 *
 * `toolStates` is the authoritative per-execution status list (one card per
 * previously executed tool). Arguments come from `tool_call` entries and the
 * result from `tool_result` entries; both are aligned by index because the
 * transcript emits them in execution order. Mismatches degrade gracefully:
 * a tool still renders with its name + status even if args/result are absent
 * (e.g. a tool that was approved but not yet executed).
 */
export const buildRuntimeToolTrace = (
  entries: RuntimeV2Entry[],
  toolStates: RuntimeV2ToolState[],
): RuntimeToolItem[] => {
  const calls = entries
    .filter((entry) => entry.type === "tool_call")
    .map((entry) => ({
      toolName: entry.data.toolName ?? "工具",
      arguments: entry.data.arguments,
    }));

  const results = entries
    .filter((entry) => entry.type === "tool_result")
    .map((entry) => ({
      content: entry.data.content ?? "",
      errorCode: entry.data.errorCode ?? null,
    }));

  return toolStates.map((tool, index) => {
    const call = calls[index];
    const result = results[index];
    const resultText = result?.content ?? "";
    return {
      key: tool.id ?? `tool-${index}`,
      toolName: tool.toolName || call?.toolName || "工具",
      phase: toolStatePhase(tool.status),
      arguments: call?.arguments,
      result: resultText,
      errorCode: result?.errorCode ?? null,
      hasArgs: call?.arguments !== undefined,
      hasResult: resultText !== "",
    };
  });
};