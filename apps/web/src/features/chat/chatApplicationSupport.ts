import type { LiveTurn, RuntimeV2Snapshot } from "./apiTypes";
import { runtimeStatusToTurnStatus } from "./conversationSnapshotMapper";

// 从 `useChatApplication.ts` 拆出的**无 React 依赖**的辅助（行为零改动）：
// localStorage 读写、地址栏 hash 解析、运行时快照 → LiveTurn 投影。
// 拆出去的理由：这些是纯函数/常量，可以单独测试，也不需要跟着 hook 的渲染逻辑走。

export const WORKSPACE_STORAGE_KEY = "endless-task.workspace";
export const ACTIVE_CONVERSATION_STORAGE_KEY = "endless-task.active-conversation";
export const VIEW_LANE_STORAGE_KEY = "endless-task.view-lanes";
export const conversationIdFromHash = (): string | null => {
  try {
    const match = /^#\/conversation\/([^/]+)/.exec(globalThis.location?.hash ?? "");
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
};


export const readStoredWorkspace = (): string | null => {
  try {
    const value = globalThis.localStorage?.getItem(WORKSPACE_STORAGE_KEY);
    return value && value !== "general" ? value : null;
  } catch {
    return null;
  }
};

// 刷新恢复上次会话：持久化最近一次激活的会话 id 及其工作区 id。
export const readStoredActiveConversation = (): {
  conversationId: string;
  workspaceId: string | null;
} | null => {
  try {
    const raw = globalThis.localStorage?.getItem(
      ACTIVE_CONVERSATION_STORAGE_KEY,
    );
    if (!raw) return null;
    const parsed = JSON.parse(raw) as {
      conversationId?: string;
      workspaceId?: string | null;
    };
    if (!parsed.conversationId) return null;
    return {
      conversationId: parsed.conversationId,
      workspaceId: parsed.workspaceId ?? null,
    };
  } catch {
    return null;
  }
};

// 刷新后仍停留在上次查看的车道：持久化「conversationId -> 当前查看的车道 id」。
// 这样在分支上刷新（或临时对话对照刷新）后，主视图不会跳回主线，避免排版突变。
export const readStoredViewLanes = (): Record<string, string> => {
  try {
    const raw = globalThis.localStorage?.getItem(VIEW_LANE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => typeof value === "string"),
    ) as Record<string, string>;
  } catch {
    return {};
  }
};

export const writeStoredViewLanes = (value: Record<string, string>): void => {
  try {
    globalThis.localStorage?.setItem(VIEW_LANE_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // 持久化失败不影响交互。
  }
};

export const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
export const activeRuntimeStatuses = new Set([
  "created",
  "queued",
  "running",
  "waiting_approval",
  "compacting",
  "cancelling",
]);

export const requestId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;

export type SnapshotTarget = "main" | "side";

export type SideLaneTarget = {
  conversationId: string;
  laneId: string;
  mode: "temporary_conversation" | "branch_lane";
};

export const pendingApprovalFromRuntime = (
  runtime: RuntimeV2Snapshot,
): LiveTurn["pendingApproval"] => {
  const approval = runtime.pendingApprovals[0];
  if (!approval) return undefined;
  return {
    id: approval.id,
    toolCallId: approval.toolExecutionId,
    summary: approval.summary,
    reason: approval.reason,
    status: "pending",
    createdAt: runtime.lastEventSeq ? new Date().toISOString() : "",
    resolvedAt: null,
    metadata: {
      toolName: approval.toolName,
      effect: approval.effect ?? null,
      risk: approval.risk ?? null,
    },
  };
};

export const liveFromRuntimeSnapshot = (runtime: RuntimeV2Snapshot): LiveTurn | null => {
  const run = runtime.runState;
  if (!run) return null;
  return {
    turnId: run.runId,
    responseVariantId: run.runId,
    status: runtimeStatusToTurnStatus(run.status),
    content: run.partialContent,
    lastSequence: runtime.lastEventSeq,
    error: run.errorCode
      ? {
          code: run.errorCode,
          message: run.safeMessage ?? "Runtime v2 执行失败。",
          retryable: false,
          correlationId: run.runId,
        }
      : undefined,
    pendingApproval: pendingApprovalFromRuntime(runtime),
    activities: runtime.toolStates.map((tool) => ({
      id: tool.id,
      status:
        tool.status === "completed"
          ? "completed"
          : tool.status === "failed"
            ? "failed"
            : tool.status === "cancelled" || tool.status === "rejected"
              ? "cancelled"
              : "running",
      message: `${tool.toolName} ${tool.status}`,
      startedAt: "",
      updatedAt: "",
    })),
  };
};
