import { describe, expect, it } from "vitest";
import type {
  RuntimeV2Entry,
  RuntimeV2ProductEvent,
  RuntimeV2ToolState,
} from "./apiTypes";
import {
  buildRunTimeline,
  buildRuntimeToolTrace,
  extractWorkspaceSourceRefs,
  toolStatusToPhase,
  type RuntimeToolItem,
} from "./runtimeTrace";

const toolState = (over: Partial<RuntimeV2ToolState>): RuntimeV2ToolState => ({
  id: "exec-1",
  runId: "run-1",
  modelTurnId: "mt-1",
  callId: "call-1",
  toolName: "read_text_file",
  status: "completed",
  errorCode: null,
  safeMessage: null,
  retryable: null,
  correlationId: null,
  errorDetails: null,
  resultEntryId: "entry-r1",
  ...over,
});

const event = (over: Partial<RuntimeV2ProductEvent>): RuntimeV2ProductEvent => ({
  eventId: "e",
  eventSeq: 1,
  type: "message.updated",
  conversationId: "c-1",
  laneId: "lane-1",
  runId: "run-1",
  createdAt: "2026-09-06T00:00:00Z",
  data: {},
  ...over,
});

const entry = (over: Partial<RuntimeV2Entry>): RuntimeV2Entry => ({
  id: "entry-x",
  type: "user_message",
  actor: "user",
  status: "final",
  createdAt: "2026-09-06T00:00:00Z",
  sourceRunId: "run-1",
  data: {},
  ...over,
});

describe("toolStatusToPhase", () => {
  it("把内部工具状态收敛到用户可读阶段", () => {
    expect(toolStatusToPhase("completed")).toBe("completed");
    expect(toolStatusToPhase("failed")).toBe("failed");
    expect(toolStatusToPhase("cancelled")).toBe("cancelled");
    expect(toolStatusToPhase("waiting_approval")).toBe("waiting");
    expect(toolStatusToPhase("created")).toBe("pending");
    expect(toolStatusToPhase("queued")).toBe("running");
    expect(toolStatusToPhase("rejected")).toBe("rejected");
    expect(toolStatusToPhase("expired")).toBe("rejected");
    expect(toolStatusToPhase("whatever")).toBe("running");
  });
});

describe("buildRunTimeline", () => {
  it("无 run/事件/非本 run 事件时返回 null", () => {
    expect(buildRunTimeline(undefined, [], "run-1")).toBeNull();
    expect(buildRunTimeline([], [], "run-1")).toBeNull();
    expect(buildRunTimeline([event({ runId: "other" })], [], "run-1")).toBeNull();
  });

  it("无工具卡时返回 null（调用方回退普通布局）", () => {
    const timeline = buildRunTimeline(
      [
        event({ eventSeq: 1, type: "message.updated", data: { delta: "你好" } }),
      ],
      [],
      "run-1",
    );
    expect(timeline).toBeNull();
  });

  it("按 eventSeq 交错文本与工具，工具用快照字段兜底", () => {
    const timeline = buildRunTimeline(
      [
        event({
          eventSeq: 1,
          type: "message.updated",
          data: { delta: "我来读取文件" },
        }),
        event({
          eventSeq: 2,
          type: "tool_execution.started",
          data: { toolExecutionId: "exec-9", status: "running" },
        }),
        event({
          eventSeq: 3,
          type: "message.updated",
          data: { delta: "，结果如下" },
        }),
      ],
      [],
      "run-1",
    );
    expect(timeline).not.toBeNull();
    expect(timeline!.map((item) => item.kind)).toEqual([
      "text",
      "tool",
      "text",
    ]);
    const toolItem = timeline![1];
    if (toolItem.kind !== "tool") throw new Error("expected tool");
    expect(toolItem.tool.phase).toBe("running");
    expect(toolItem.tool.toolName).toBe("工具");
    // 文本 delta 累积
    const texts = timeline!.filter((item) => item.kind === "text");
    expect(texts[0].kind === "text" && texts[0].text).toBe("我来读取文件");
    expect(texts[1].kind === "text" && texts[1].text).toBe("，结果如下");
  });

  it("快照中的同名工具卡优先于事件字段", () => {
    const timeline = buildRunTimeline(
      [
        event({
          eventSeq: 1,
          type: "tool_execution.updated",
          data: { toolExecutionId: "exec-1", status: "completed" },
        }),
      ],
      [
      {
        key: "exec-1",
        toolName: "read_text_file",
        phase: "completed",
        isError: false,
        hasArgs: false,
        hasResult: true,
      },
    ],
      "run-1",
    );
    const tool = timeline!.find((item) => item.kind === "tool");
    expect(tool && tool.kind === "tool" && tool.tool.toolName).toBe(
      "read_text_file",
    );
  });

  it("failed/errorCode 标记 isError", () => {
    const timeline = buildRunTimeline(
      [
        event({
          eventSeq: 1,
          type: "tool_execution.failed",
          data: { toolExecutionId: "exec-2", status: "failed" },
        }),
      ],
      [],
      "run-1",
    );
    const tool = timeline!.find((item) => item.kind === "tool");
    expect(tool && tool.kind === "tool" && tool.tool.isError).toBe(true);
  });

  it("同一 toolExecutionId 的多个事件只渲染一张卡（去重，问题一）", () => {
    const timeline = buildRunTimeline(
      [
        event({
          eventSeq: 1,
          type: "tool_execution.updated",
          data: { toolExecutionId: "exec-x", status: "created", toolName: "list_workspace_dir" },
        }),
        event({
          eventSeq: 2,
          type: "tool_execution.started",
          data: { toolExecutionId: "exec-x", status: "running", toolName: "list_workspace_dir" },
        }),
        event({
          eventSeq: 3,
          type: "tool_execution.completed",
          data: { toolExecutionId: "exec-x", status: "completed", toolName: "list_workspace_dir" },
        }),
        event({
          eventSeq: 4,
          type: "tool_execution.updated",
          data: { toolExecutionId: "exec-y", status: "completed", toolName: "read_text_file" },
        }),
      ],
      [],
      "run-1",
    );
    const tools = timeline!.filter((item) => item.kind === "tool");
    expect(tools).toHaveLength(2);
    expect(tools[0].kind === "tool" && tools[0].id).toBe("tool-exec-x");
    expect(tools[1].kind === "tool" && tools[1].id).toBe("tool-exec-y");
    // 去重后每张卡 id（React key）唯一
    const ids = tools.map((item) => (item.kind === "tool" ? item.id : ""));
    expect(new Set(ids).size).toBe(2);
  });

  it("tool_execution.progress 不新起工具卡", () => {
    const timeline = buildRunTimeline(
      [
        event({
          eventSeq: 1,
          type: "tool_execution.started",
          data: { toolExecutionId: "exec-z", status: "running", toolName: "run_shell" },
        }),
        event({
          eventSeq: 2,
          type: "tool_execution.progress",
          data: { toolExecutionId: "exec-z", message: "50%" },
        }),
      ],
      [],
      "run-1",
    );
    const tools = timeline!.filter((item) => item.kind === "tool");
    expect(tools).toHaveLength(1);
  });
});

describe("buildRuntimeToolTrace", () => {
  it("按 callId 关联调用参数与结果", () => {
    const entries: RuntimeV2Entry[] = [
      entry({
        type: "tool_call",
        id: "c1",
        data: { callId: "call-1", toolName: "run_shell", arguments: { cmd: "ls" } },
      }),
      entry({
        type: "tool_result",
        id: "r1",
        data: {
          callId: "call-1",
          content: "file.txt",
          errorCode: null,
          structuredContent: { exitCode: 0 },
        },
      }),
    ];
    const trace = buildRuntimeToolTrace(entries, [
      toolState({ callId: "call-1", toolName: "run_shell", status: "completed" }),
    ]);
    expect(trace).toHaveLength(1);
    expect(trace[0].toolName).toBe("run_shell");
    expect(trace[0].arguments).toEqual({ cmd: "ls" });
    expect(trace[0].result).toBe("file.txt");
    expect(trace[0].structuredContent).toEqual({ exitCode: 0 });
    expect(trace[0].hasArgs).toBe(true);
    expect(trace[0].hasResult).toBe(true);
    expect(trace[0].isError).toBe(false);
  });

  it("缺调用/结果时优雅降级（仍有名字与状态）", () => {
    const trace = buildRuntimeToolTrace([], [
      toolState({ callId: "missing", toolName: "read_workspace_file", status: "running" }),
    ]);
    expect(trace).toHaveLength(1);
    expect(trace[0].toolName).toBe("read_workspace_file");
    expect(trace[0].phase).toBe("running");
    expect(trace[0].hasArgs).toBe(false);
    expect(trace[0].hasResult).toBe(false);
  });
});

const traceTool = (over: Partial<RuntimeToolItem>): RuntimeToolItem => ({
  key: "exec-1",
  toolName: "read_workspace_file",
  phase: "completed",
  arguments: undefined,
  result: "",
  structuredContent: undefined,
  errorCode: null,
  isError: false,
  hasArgs: false,
  hasResult: false,
  ...over,
});

describe("extractWorkspaceSourceRefs", () => {
  it("从 read_workspace_file 结果提取 path 与行范围", () => {
    const refs = extractWorkspaceSourceRefs([
      traceTool({
        toolName: "read_workspace_file",
        structuredContent: {
          path: "docs/plan.md",
          startLine: 10,
          endLine: 14,
          totalLines: 50,
          truncated: false,
        },
      }),
    ]);
    expect(refs).toHaveLength(1);
    expect(refs[0]).toMatchObject({
      toolName: "read_workspace_file",
      path: "docs/plan.md",
      startLine: 10,
      endLine: 14,
      totalLines: 50,
      truncated: false,
    });
  });

  it("合并连续工具结果的路径范围去重", () => {
    const refs = extractWorkspaceSourceRefs([
      traceTool({
        structuredContent: {
          path: "a.md",
          startLine: 1,
          endLine: 5,
          totalLines: 10,
        },
      }),
      traceTool({
        structuredContent: {
          path: "a.md",
          startLine: 1,
          endLine: 5,
          totalLines: 10,
        },
      }),
      traceTool({
        structuredContent: {
          path: "b.md",
          startLine: 2,
          endLine: 2,
          totalLines: 20,
        },
      }),
    ]);
    expect(refs).toHaveLength(2);
    expect(refs.map((ref) => ref.path)).toEqual(["a.md", "b.md"]);
  });

  it("忽略非 read/search 工具与无 path 结果", () => {
    const refs = extractWorkspaceSourceRefs([
      traceTool({ toolName: "run_shell", structuredContent: { path: "x" } }),
      traceTool({ structuredContent: { startLine: 1, endLine: 2 } }),
    ]);
    expect(refs).toEqual([]);
  });

  it("workspace_search 无行范围时退化为第 1 行", () => {
    const refs = extractWorkspaceSourceRefs([
      traceTool({
        toolName: "workspace_search",
        structuredContent: { path: "notes.txt", zeroHit: false },
      }),
    ]);
    expect(refs).toHaveLength(1);
    expect(refs[0]).toMatchObject({ path: "notes.txt", startLine: 1, endLine: 1 });
  });
});
