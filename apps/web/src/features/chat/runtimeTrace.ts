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
/** 时间线渲染分组：文本片段照常展示，连续的工具卡合并成一组（默认折叠）。 */
export type TimelineRenderGroup =
  | { kind: "text"; key: string; text: string }
  | { kind: "tools"; key: string; tools: RuntimeToolItem[] };

/**
 * 把交错时间线折成"文本 / 工具组"序列：连续的工具卡合成一组，便于 UI 默认折叠，
 * 同时保证回答正文（text 项）始终可见。
 */
export const groupTimeline = (
  items: LiveTimelineItem[] | null | undefined,
): TimelineRenderGroup[] => {
  if (!items?.length) return [];
  const groups: TimelineRenderGroup[] = [];
  let buffer: RuntimeToolItem[] = [];
  const flush = (index: number) => {
    if (!buffer.length) return;
    groups.push({ kind: "tools", key: `tools-${index}`, tools: buffer });
    buffer = [];
  };
  items.forEach((item, index) => {
    if (item.kind === "tool") {
      buffer.push(item.tool);
      return;
    }
    flush(index);
    groups.push({ kind: "text", key: item.id, text: item.text });
  });
  flush(items.length);
  return groups;
};

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
  // execId -> 时间线中工具卡的下标。后端一次工具执行会下发多个
  // `tool_execution.*` 产品事件（created/status_changed/started/completed 等），
  // 必须按 execId 合并成一张卡，否则会出现重复工具卡（见 issue：#问题一）。
  const toolCardIndex = new Map<string, number>();
  let text = "";
  const flush = (force: boolean) => {
    if (text.trim() || force) {
      items.push({ id: `text-${items.length}`, kind: "text", text });
      text = "";
    }
  };

  // 卡片内容优先用快照 toolStates 的权威最终态（避免把“跳过的事件状态”误画成中间色），
  // 快照没有时再退化为事件字段（运行中的实时展示）。
  const resolveTool = (execId: string, event: RuntimeV2ProductEvent): RuntimeToolItem => {
    const snapshotTool = toolByExec.get(execId);
    if (snapshotTool) return snapshotTool;
    const status = String(event.data.status ?? "running");
    return {
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
    };
  };

  for (const event of runEvents) {
    if (event.type === "message.updated") {
      text += event.data.delta ?? "";
    } else if (event.type.startsWith("tool_execution.")) {
      // 纯进度事件不该新起一张卡（可能高频、无状态语义），只参与已有卡的合并。
      if (event.type === "tool_execution.progress") continue;
      if (!event.data.toolExecutionId) continue;
      const execId = String(event.data.toolExecutionId);
      const existingIndex = toolCardIndex.get(execId);
      if (existingIndex === undefined) {
        flush(false);
        items.push({
          id: `tool-${execId}`,
          kind: "tool",
          tool: resolveTool(execId, event),
        });
        toolCardIndex.set(execId, items.length - 1);
      } else {
        // 同一次工具执行的后续事件原地更新（用快照终态覆盖中间态），不再新增卡片。
        const item = items[existingIndex];
        if (item.kind === "tool") item.tool = resolveTool(execId, event);
      }
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

/** 一个工作区文件来源（read_workspace_file / workspace_search 的可溯源引用）。 */
export type WorkspaceSourceRef = {
  key: string;
  toolName: string;
  path: string;
  startLine: number;
  endLine: number;
  totalLines: number | null;
  truncated: boolean;
};

const WORKSPACE_READ_TOOLS = new Set(["read_workspace_file", "workspace_search"]);

function readRefFromStructured(raw: unknown): {
  path?: string;
  startLine?: number;
  endLine?: number;
  totalLines?: number | null;
  truncated?: boolean;
} {
  if (typeof raw !== "object" || raw === null) return {};
  const value = raw as Record<string, unknown>;
  const pick = (key: string) => {
    const item = value[key];
    return typeof item === "number" ? item : undefined;
  };
  const path = typeof value.path === "string" ? value.path : undefined;
  return {
    path,
    startLine: pick("startLine"),
    endLine: pick("endLine"),
    totalLines:
      value.totalLines === null || value.totalLines === undefined
        ? null
        : pick("totalLines"),
    truncated: value.truncated === true,
  };
}

/**
 * 从某个 turn 的工具执行结果里提取工作区文件来源（read_workspace_file /
 * workspace_search 的 path + 行范围），按出现顺序去重，供「回答使用了哪些
 * 文件」溯源引用展示。只读来源不依赖模型是否在正文里写了 [K…]。
 */
export const extractWorkspaceSourceRefs = (
  tools: RuntimeToolItem[],
): WorkspaceSourceRef[] => {
  const seen = new Set<string>();
  const refs: WorkspaceSourceRef[] = [];
  for (const tool of tools) {
    if (!WORKSPACE_READ_TOOLS.has(tool.toolName)) continue;
    const structured = tool.structuredContent;
    const meta = readRefFromStructured(structured);
    if (!meta.path) continue;
    // workspace_search 不带行范围（命中行在 matches 里）；read 才有连续范围。
    const startLine = meta.startLine ?? 1;
    const endLine = meta.endLine ?? startLine;
    const key = `${tool.toolName}:${meta.path}:${startLine}-${endLine}`;
    if (seen.has(key)) continue;
    seen.add(key);
    refs.push({
      key: `${tool.toolName}:${meta.path}`,
      toolName: tool.toolName,
      path: meta.path,
      startLine,
      endLine,
      totalLines: meta.totalLines ?? null,
      truncated: Boolean(meta.truncated),
    });
  }
  return refs;
};
