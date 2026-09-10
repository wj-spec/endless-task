// @vitest-environment jsdom
/**
 * `chatApplicationSupport`：从 `useChatApplication.ts` 拆出的无 React 依赖辅助。
 *
 * 这些函数此前只能通过"渲染整个 hook + 读 localStorage"间接覆盖，拆出来后可以直测。
 * 用例同时充当**搬家行为不变**的证据：断言的是移动前后的既有语义（含"解析失败一律
 * 当作没有"这类静默兜底），而不是新写的期望。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { RuntimeV2Snapshot } from "./apiTypes";
import {
  ACTIVE_CONVERSATION_STORAGE_KEY,
  VIEW_LANE_STORAGE_KEY,
  WORKSPACE_STORAGE_KEY,
  conversationIdFromHash,
  liveFromRuntimeSnapshot,
  readStoredActiveConversation,
  readStoredViewLanes,
  readStoredWorkspace,
  writeStoredViewLanes,
} from "./chatApplicationSupport";

beforeEach(() => {
  globalThis.localStorage.clear();
  globalThis.location.hash = "";
});

afterEach(() => {
  globalThis.localStorage.clear();
});

describe("conversationIdFromHash", () => {
  it("从 #/conversation/<id> 解析会话 id（并解码百分号转义）", () => {
    globalThis.location.hash = "#/conversation/conv_abc";
    expect(conversationIdFromHash()).toBe("conv_abc");
    globalThis.location.hash = "#/conversation/conv%20a";
    expect(conversationIdFromHash()).toBe("conv a");
  });

  it("非会话路由与空 hash 返回 null", () => {
    globalThis.location.hash = "";
    expect(conversationIdFromHash()).toBeNull();
    globalThis.location.hash = "#/settings";
    expect(conversationIdFromHash()).toBeNull();
  });
});

describe("localStorage 读取（解析失败一律当作没有）", () => {
  it("工作区：普通值可用，general 视为未选择", () => {
    expect(readStoredWorkspace()).toBeNull();
    globalThis.localStorage.setItem(WORKSPACE_STORAGE_KEY, "ws_1");
    expect(readStoredWorkspace()).toBe("ws_1");
    globalThis.localStorage.setItem(WORKSPACE_STORAGE_KEY, "general");
    expect(readStoredWorkspace()).toBeNull();
  });

  it("活动会话：合法 JSON 解析出 id，坏 JSON 与缺字段返回 null", () => {
    expect(readStoredActiveConversation()).toBeNull();
    globalThis.localStorage.setItem(
      ACTIVE_CONVERSATION_STORAGE_KEY,
      JSON.stringify({ conversationId: "conv_1", workspaceId: "ws_1" }),
    );
    expect(readStoredActiveConversation()).toEqual({
      conversationId: "conv_1",
      workspaceId: "ws_1",
    });
    globalThis.localStorage.setItem(ACTIVE_CONVERSATION_STORAGE_KEY, "{不是 JSON");
    expect(readStoredActiveConversation()).toBeNull();
    globalThis.localStorage.setItem(
      ACTIVE_CONVERSATION_STORAGE_KEY,
      JSON.stringify({ workspaceId: "ws_1" }),
    );
    expect(readStoredActiveConversation()).toBeNull();
  });

  it("查看车道：缺省为空表，坏 JSON 也退化成空表；写入可回读", () => {
    expect(readStoredViewLanes()).toEqual({});
    globalThis.localStorage.setItem(VIEW_LANE_STORAGE_KEY, "坏");
    expect(readStoredViewLanes()).toEqual({});
    writeStoredViewLanes({ conv_1: "lane_a", conv_2: "lane_b" });
    expect(readStoredViewLanes()).toEqual({ conv_1: "lane_a", conv_2: "lane_b" });
  });
});

describe("liveFromRuntimeSnapshot（运行时快照 → 实时轮次）", () => {
  const baseSnapshot = (overrides: Partial<RuntimeV2Snapshot> = {}): RuntimeV2Snapshot =>
    ({
      conversationId: "conv_1",
      lastEventSeq: 7,
      runState: null,
      toolStates: [],
      pendingApprovals: [],
      ...overrides,
    }) as RuntimeV2Snapshot;

  it("没有运行态时返回 null", () => {
    expect(liveFromRuntimeSnapshot(baseSnapshot())).toBeNull();
  });

  it("运行中：投影状态、局部内容与工具活动", () => {
    const live = liveFromRuntimeSnapshot(
      baseSnapshot({
        runState: {
          runId: "run_1",
          status: "running",
          partialContent: "半截回答",
        },
        toolStates: [
          { id: "t1", toolName: "read_workspace_file", status: "completed" },
          { id: "t2", toolName: "write_workspace_file", status: "failed" },
          { id: "t3", toolName: "delete_workspace_file", status: "rejected" },
          { id: "t4", toolName: "list_workspace_dir", status: "running" },
        ],
      } as Partial<RuntimeV2Snapshot>),
    );
    expect(live).not.toBeNull();
    expect(live!.turnId).toBe("run_1");
    expect(live!.responseVariantId).toBe("run_1");
    expect(live!.status).toBe("running");
    expect(live!.content).toBe("半截回答");
    expect(live!.lastSequence).toBe(7);
    expect(live!.activities.map((item) => item.status)).toEqual([
      "completed",
      "failed",
      "cancelled",
      "running",
    ]);
    // 未结束的轮次没有错误与审批
    expect(live!.error).toBeUndefined();
    expect(live!.pendingApproval).toBeUndefined();
  });

  it("失败运行带上可读错误，等待审批时投影出审批信息", () => {
    const live = liveFromRuntimeSnapshot(
      baseSnapshot({
        runState: {
          runId: "run_2",
          status: "failed",
          errorCode: "provider_unavailable",
        },
        pendingApprovals: [
          {
            id: "appr_1",
            toolExecutionId: "texec_1",
            toolName: "write_workspace_file",
            summary: "写入文件",
            reason: "需要授权",
            effect: "local_write",
            risk: "medium",
          },
        ],
      } as Partial<RuntimeV2Snapshot>),
    );
    expect(live!.status).toBe("failed");
    expect(live!.error).toEqual({
      code: "provider_unavailable",
      message: "Runtime v2 执行失败。",
      retryable: false,
      correlationId: "run_2",
    });
    expect(live!.pendingApproval).toMatchObject({
      id: "appr_1",
      toolCallId: "texec_1",
      status: "pending",
      metadata: {
        toolName: "write_workspace_file",
        effect: "local_write",
        risk: "medium",
      },
    });
  });
});
