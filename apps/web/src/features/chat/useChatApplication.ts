import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiClientError,
  chatApi,
} from "./api";
import type {
  CapabilitySnapshot,
  ActivityStatus,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  LiveTurn,
  ProviderProfile,
  RuntimeV2Entry,
  RuntimeV2Lane,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  Message,
  TurnStatus,
  Workspace,
} from "./apiTypes";
import { readableError } from "./apiErrorText";
import {
  runtimeTargetKey,
  useConversationRuntimeController,
} from "./runtimeController";

const WORKSPACE_STORAGE_KEY = "endless-task.workspace";
const ACTIVE_CONVERSATION_STORAGE_KEY = "endless-task.active-conversation";
const VIEW_LANE_STORAGE_KEY = "endless-task.view-lanes";
const conversationIdFromHash = (): string | null => {
  try {
    const match = /^#\/conversation\/([^/]+)/.exec(globalThis.location?.hash ?? "");
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
};


const readStoredWorkspace = (): string | null => {
  try {
    const value = globalThis.localStorage?.getItem(WORKSPACE_STORAGE_KEY);
    return value && value !== "general" ? value : null;
  } catch {
    return null;
  }
};

// 刷新恢复上次会话：持久化最近一次激活的会话 id 及其工作区 id。
const readStoredActiveConversation = (): {
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
const readStoredViewLanes = (): Record<string, string> => {
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

const writeStoredViewLanes = (value: Record<string, string>): void => {
  try {
    globalThis.localStorage?.setItem(VIEW_LANE_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // 持久化失败不影响交互。
  }
};

const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
const activeRuntimeStatuses = new Set([
  "created",
  "queued",
  "running",
  "waiting_approval",
  "compacting",
  "cancelling",
]);

import {
  runtimeSnapshotToConversationSnapshot,
  runtimeStatusToTurnStatus,
} from "./conversationSnapshotMapper";
const requestId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;

type SnapshotTarget = "main" | "side";

type SideLaneTarget = {
  conversationId: string;
  laneId: string;
  mode: "temporary_conversation" | "branch_lane";
};

const pendingApprovalFromRuntime = (
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

const liveFromRuntimeSnapshot = (runtime: RuntimeV2Snapshot): LiveTurn | null => {
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

export function useChatApplication() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ConversationSnapshot>>({});
  const [sideSnapshots, setSideSnapshots] = useState<
    Record<string, ConversationSnapshot>
  >({});
  const [liveTurns, setLiveTurns] = useState<Record<string, LiveTurn>>({});
  const [editedUserMessages, setEditedUserMessages] = useState<
    Record<string, string>
  >({});
  const [sideConversationId, setSideConversationId] = useState<string | null>(null);
  const [sideLane, setSideLane] = useState<SideLaneTarget | null>(null);
  const [sideDraft, setSideDraft] = useState("");
  const [sideLoading, setSideLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ConversationStatus>("active");
  const [search, setSearch] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [homePath, setHomePath] = useState("");
  const [workspaceId, setWorkspaceId] = useState<string | null>(
    readStoredWorkspace,
  );
  // 当前工作区上下文：仅当它是「已绑定本地目录」的工作区时才允许新建会话。
  const currentWorkspace = useMemo(
    () => workspaces.find((item) => item.id === workspaceId) ?? null,
    [workspaces, workspaceId],
  );
  const workspaceCanCreate = Boolean(currentWorkspace?.rootPath);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null);
  const [mainLaneIds, setMainLaneIds] = useState<Record<string, string>>({});
  const [viewLaneIds, setViewLaneIds] = useState<Record<string, string>>(() =>
    readStoredViewLanes(),
  );
  const [laneTrees, setLaneTrees] = useState<Record<string, RuntimeV2Lane[]>>({});
  const streams = useRef(new Map<string, AbortController>());
  const hasOpenedConversationRef = useRef(false);
  const conversationListVersion = useRef(0);
  const runtimeController = useConversationRuntimeController();
  const runtimeSnapshotBindings = useRef(
    new Map<string, Set<SnapshotTarget>>(),
  );
  const appliedRuntimeSnapshots = useRef(
    new Map<string, RuntimeV2Snapshot>(),
  );
  const primarySurfaceRequestVersion = useRef(0);
  const sideRequestVersion = useRef(0);
  const activeRuntimeLaneId = activeConversationId
    ? (viewLaneIds[activeConversationId] ?? mainLaneIds[activeConversationId] ?? null)
    : null;
  const primaryCommandTarget = activeConversationId
    ? { conversationId: activeConversationId, laneId: activeRuntimeLaneId }
    : null;
  const sideCommandTarget = sideConversationId
    ? {
        conversationId: sideConversationId,
        laneId: sideLane?.laneId ?? null,
      }
    : null;
  const primaryCommandState = primaryCommandTarget
    ? runtimeController.commands[runtimeTargetKey(primaryCommandTarget)]
    : undefined;
  const sideCommandState = sideCommandTarget
    ? runtimeController.commands[runtimeTargetKey(sideCommandTarget)]
    : undefined;

  const setCommandFeedback = (
    target: { conversationId: string; laneId: string | null } | null,
    nextPendingAction: string | null,
    nextError: string | null,
  ) => {
    if (target) {
      runtimeController.setCommand(target, nextPendingAction, nextError);
      return;
    }
    setPendingAction(nextPendingAction);
    setError(nextError);
  };
  const dismissPrimaryError = () => {
    if (primaryCommandTarget && primaryCommandState) {
      runtimeController.setCommand(
        primaryCommandTarget,
        primaryCommandState.pendingAction,
        null,
      );
    } else {
      setError(null);
    }
  };
  const dismissSideError = () => {
    if (!sideCommandTarget) return;
    runtimeController.setCommand(
      sideCommandTarget,
      sideCommandState?.pendingAction ?? null,
      null,
    );
  };
  const unbindRuntimeTarget = useCallback(
    (target: { conversationId: string; laneId: string | null }, surface: SnapshotTarget) => {
      const key = runtimeTargetKey(target);
      const bindings = runtimeSnapshotBindings.current.get(key);
      bindings?.delete(surface);
      appliedRuntimeSnapshots.current.delete(`${surface}:${key}`);
      if (bindings?.size) {
        runtimeSnapshotBindings.current.set(key, bindings);
        return;
      }
      runtimeSnapshotBindings.current.delete(key);
      runtimeController.removeTarget(target);
    },
    [runtimeController.removeTarget],
  );

  const applyRuntimeSnapshot = useCallback(
    (
      legacy: ConversationSnapshot,
      runtime: RuntimeV2Snapshot,
      target: SnapshotTarget = "main",
    ) => {
      const conversationId = legacy.conversation.id;
      const snapshot = runtimeSnapshotToConversationSnapshot(legacy, runtime);
      if (target === "main") {
        setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
        setConversations((current) =>
          current.map((item) =>
            item.id === conversationId ? snapshot.conversation : item,
          ),
        );
      } else {
        setSideSnapshots((current) => ({
          ...current,
          [runtime.activeLaneId ?? "active"]: snapshot,
        }));
      }

      const live = liveFromRuntimeSnapshot(runtime);
      setLiveTurns((current) => {
        const next = { ...current };
        for (const turn of snapshot.turns) {
          if (!activeRuntimeStatuses.has(turn.turn.status)) delete next[turn.turn.id];
        }
        if (live) next[live.turnId] = live;
        return next;
      });
    },
    [],
  );

  const applyRuntimeProductEvent = useCallback((event: RuntimeV2ProductEvent) => {
    const runId = event.runId;
    if (!runId) return;
    setLiveTurns((current) => {
      const previous = current[runId];
      const next: LiveTurn = {
        turnId: runId,
        responseVariantId: runId,
        status: previous?.status ?? "running",
        content: previous?.content ?? "",
        lastSequence: Math.max(previous?.lastSequence ?? 0, event.eventSeq),
        error: previous?.error,
        pendingApproval: previous?.pendingApproval,
        activities: previous?.activities ?? [],
      };

      if (event.type === "message.updated") next.content += event.data.delta ?? "";
      if (event.type === "run.started" || event.type === "run.status_changed") {
        next.status = runtimeStatusToTurnStatus(
          String(event.data.status ?? "running"),
        );
      }
      if (event.type === "run.finished") next.status = "completed";
      if (event.type === "run.cancelled") next.status = "cancelled";
      if (event.type === "run.failed") {
        next.status = "failed";
        next.error = {
          code: String(event.data.errorCode ?? "runtime_failed"),
          message: String(event.data.safeMessage ?? "Runtime v2 执行失败。"),
          retryable: false,
          correlationId: event.eventId,
        };
      }
      if (
        event.type === "approval.requested" &&
        event.data.approvalId &&
        event.data.summary &&
        event.data.reason
      ) {
        next.pendingApproval = {
          id: String(event.data.approvalId),
          toolCallId: String(event.data.toolExecutionId ?? ""),
          summary: String(event.data.summary),
          reason: String(event.data.reason),
          status: "pending",
          createdAt: event.createdAt,
          resolvedAt: null,
          metadata: {
            toolName: String(event.data.toolName ?? ""),
            effect: String(event.data.effect ?? "") || null,
            risk: (event.data.risk as "low" | "medium" | "high" | undefined) ?? null,
          },
        };
      }
      if (event.type === "approval.resolved" && event.data.approvalId) {
        next.pendingApproval = undefined;
      }
      if (event.type.startsWith("tool_execution.") && event.data.toolExecutionId) {
        const toolId = String(event.data.toolExecutionId);
        const rawStatus = event.data.status ? String(event.data.status) : "running";
        const activityStatus: ActivityStatus =
          rawStatus === "completed"
            ? "completed"
            : rawStatus === "failed"
              ? "failed"
              : rawStatus === "running"
                ? "running"
                : "cancelled";
        const previousActivity = next.activities.find((item) => item.id === toolId);
        const toolStatusTail: Record<string, string> = {
          created: "正在执行…",
          queued: "等待执行…",
          started: "正在执行…",
          running: "正在执行…",
          completed: "已完成",
          failed: "执行失败",
          cancelled: "已取消",
          rejected: "未获批准",
          expired: "等待确认超时，已跳过",
        };
        const toolName = event.data.toolName ? String(event.data.toolName) : null;
        const activity = {
          id: toolId,
          status: activityStatus,
          message: `工具${toolName ? `「${toolName}」` : ""}${
            toolStatusTail[rawStatus] ?? "已结束"
          }`,
          startedAt: previousActivity?.startedAt ?? event.createdAt,
          updatedAt: event.createdAt,
        };
        next.activities = previousActivity
          ? next.activities.map((item) => (item.id === toolId ? activity : item))
          : [...next.activities, activity];
      }
      return { ...current, [runId]: next };
    });
  }, []);

  const followRuntimeConversation = useCallback(
    (
      conversationId: string,
      initialSequence: number,
      laneId?: string | null,
      target: SnapshotTarget = "main",
    ) => {
      const runtimeTarget = { conversationId, laneId: laneId ?? null };
      const key = runtimeTargetKey(runtimeTarget);
      const bindings = runtimeSnapshotBindings.current.get(key) ?? new Set();
      bindings.add(target);
      runtimeSnapshotBindings.current.set(key, bindings);
      runtimeController.followConversation(conversationId, initialSequence);
    },
    [runtimeController.followConversation],
  );

  const appliedRuntimeEventId = useRef<string | null>(null);

  useEffect(() => {
    for (const [key, runtime] of Object.entries(runtimeController.snapshots)) {
      const bindings = runtimeSnapshotBindings.current.get(key);
      if (!bindings?.size) continue;
      for (const target of bindings) {
        const legacy =
          target === "main"
            ? snapshots[runtime.conversationId]
            : sideSnapshots[runtime.activeLaneId ?? "active"];
        if (!legacy) continue;
        const bindingKey = `${target}:${key}`;
        if (appliedRuntimeSnapshots.current.get(bindingKey) === runtime) continue;
        appliedRuntimeSnapshots.current.set(bindingKey, runtime);
        applyRuntimeSnapshot(legacy, runtime, target);
      }
    }
  }, [
    applyRuntimeSnapshot,
    runtimeController.snapshots,
    sideSnapshots,
    snapshots,
  ]);

  useEffect(() => {
    const event = runtimeController.latestEvent;
    if (!event || appliedRuntimeEventId.current === event.eventId) return;
    appliedRuntimeEventId.current = event.eventId;
    applyRuntimeProductEvent(event);
  }, [applyRuntimeProductEvent, runtimeController.latestEvent]);

  const hydrateActiveTurn = useCallback(
    async (
      legacy: ConversationSnapshot,
      target: SnapshotTarget = "main",
      isCurrent: () => boolean = () => true,
    ) => {
      const laneList = await chatApi.listRuntimeV2Lanes(legacy.conversation.id);
      if (!isCurrent()) return;
      const mainLane =
        laneList.items.find((item: RuntimeV2Lane) => item.isMain) ??
        laneList.items.find(
          (item: RuntimeV2Lane) => item.id === laneList.activeLaneId,
        ) ??
        null;
      // 刷新后恢复上次查看的车道：若该车道仍存在且未归档，则停留在它上面，
      // 否则回退主线。这样在分支/对照上刷新不会跳回主线导致排版突变。
      // viewLaneIds 以存档惰性初始化，这里用当前状态值（而非重新读 storage，避免
      // 被挂载时的持久化 effect 清掉）。
      const viewLaneId = viewLaneIds[legacy.conversation.id];
      const storedViewLane =
        viewLaneId && viewLaneId !== mainLane?.id
          ? (laneList.items.find(
              (item) =>
                item.id === viewLaneId &&
                !item.isMain &&
                item.status !== "archived",
            ) ?? null)
          : null;
      const resolvedViewLane = storedViewLane ?? mainLane;
      if (mainLane) {
        setMainLaneIds((current) => ({
          ...current,
          [legacy.conversation.id]: mainLane.id,
        }));
      }
      const resolvedViewLaneId = resolvedViewLane?.id ?? mainLane?.id ?? null;
      if (resolvedViewLaneId) {
        setViewLaneIds((current) => ({
          ...current,
          [legacy.conversation.id]: resolvedViewLaneId,
        }));
      }
      setLaneTrees((current) => ({
        ...current,
        [legacy.conversation.id]: laneList.items,
      }));
      const runtime = await runtimeController.loadSnapshot({
        conversationId: legacy.conversation.id,
        laneId: resolvedViewLane?.id ?? mainLane?.id ?? null,
      });
      if (!isCurrent()) return;
      applyRuntimeSnapshot(legacy, runtime, target);
      if (runtime.runningRunId) {
        followRuntimeConversation(
          legacy.conversation.id,
          runtime.lastEventSeq,
          resolvedViewLane?.id ?? mainLane?.id,
          target,
        );
      }
    },
    [applyRuntimeSnapshot, followRuntimeConversation, viewLaneIds],
  );

  // 持久化「当前查看的车道」，使刷新后能恢复（见 hydrateActiveTurn 的恢复逻辑）。
  useEffect(() => {
    writeStoredViewLanes(viewLaneIds);
  }, [viewLaneIds]);

  const loadConversation = useCallback(
    async (conversationId: string) => {
      const requestVersion = primarySurfaceRequestVersion.current + 1;
      primarySurfaceRequestVersion.current = requestVersion;
      const isCurrent = () => primarySurfaceRequestVersion.current === requestVersion;
      setLoading(true);
      setError(null);
      try {
        const snapshot = await chatApi.getConversation(conversationId);
        if (!isCurrent()) return;
        setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
        await hydrateActiveTurn(snapshot, "main", isCurrent);
      } catch (loadError) {
        if (isCurrent()) setError(readableError(loadError));
      } finally {
        if (isCurrent()) setLoading(false);
      }
    },
    [hydrateActiveTurn],
  );

  const openConversation = useCallback(
    async (conversationId: string) => {
      hasOpenedConversationRef.current = true;
      setActiveConversationId(conversationId);
      await loadConversation(conversationId);
    },
    [loadConversation],
  );

  const openSideConversation = useCallback(
    async (conversationId: string) => {
      const requestVersion = sideRequestVersion.current + 1;
      sideRequestVersion.current = requestVersion;
      const isCurrent = () => sideRequestVersion.current === requestVersion;
      const target = { conversationId, laneId: null };
      setSideConversationId(conversationId);
      setSideDraft("");
      setSideLoading(true);
      setCommandFeedback(target, "open-conversation", null);
      try {
        const snapshot = await chatApi.getConversation(conversationId);
        if (!isCurrent()) return;
        setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
        await hydrateActiveTurn(snapshot, "main", isCurrent);
        if (isCurrent()) setCommandFeedback(target, null, null);
      } catch (loadError) {
        if (isCurrent()) {
          setCommandFeedback(target, null, readableError(loadError));
        }
      } finally {
        if (isCurrent()) setSideLoading(false);
      }
    },
    [hydrateActiveTurn],
  );

  const dismissSideConversation = useCallback((preserveTemporary = false) => {
    sideRequestVersion.current += 1;
    if (sideLane) {
      const target = {
        conversationId: sideLane.conversationId,
        laneId: sideLane.laneId,
      };
      unbindRuntimeTarget(target, "side");
      if (sideLane.mode === "temporary_conversation" && !preserveTemporary) {
        runtimeController.stopConversation(sideLane.conversationId);
      }
      setSideSnapshots((current) => {
        const next = { ...current };
        delete next[sideLane.laneId];
        return next;
      });
    }
    setSideLane(null);
    setSideConversationId(null);
  }, [
    runtimeController.stopConversation,
    sideLane,
    unbindRuntimeTarget,
  ]);

  const focusTemporaryConversation = useCallback(async () => {
    if (!sideLane || sideLane.mode !== "temporary_conversation") return false;
    await openConversation(sideLane.conversationId);
    dismissSideConversation(true);
    return true;
  }, [dismissSideConversation, openConversation, sideLane]);

  const waitForRuntimeRunToStop = useCallback(
    async (target: { conversationId: string; laneId: string | null }, runId: string) => {
      for (let attempt = 0; attempt < 25; attempt += 1) {
        const snapshot = await runtimeController.loadSnapshot(target);
        if (snapshot.runningRunId !== runId) return;
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 200));
      }
      throw new Error("运行仍在取消中，请稍后重试关闭。");
    },
    [runtimeController.loadSnapshot],
  );

  const closeSideConversation = useCallback(async () => {
    if (!sideLane) {
      dismissSideConversation();
      return true;
    }

    const target = {
      conversationId: sideLane.conversationId,
      laneId: sideLane.laneId,
    };
    const runtime = runtimeController.snapshots[runtimeTargetKey(target)];
    const runningRunId =
      runtime?.runningLaneId === sideLane.laneId ? runtime.runningRunId : null;
    if (!runningRunId && sideLane.mode === "branch_lane") {
      dismissSideConversation();
      return true;
    }

    setCommandFeedback(
      target,
      sideLane.mode === "temporary_conversation"
        ? "delete-temporary-conversation"
        : "close-running-branch",
      null,
    );
    try {
      if (runningRunId) {
        await chatApi.cancelRuntimeV2Run(runningRunId);
        await waitForRuntimeRunToStop(target, runningRunId);
      }
      if (sideLane.mode === "temporary_conversation") {
        await chatApi.deleteRuntimeV2TemporaryConversation(
          sideLane.conversationId,
        );
        try {
          const items = await chatApi.listConversations(
            statusFilter,
            undefined,
            workspaceId,
          );
          setConversations(items);
        } catch {
          // 临时对话已删除；列表刷新失败不应阻止右侧状态清理。
        }
        setMainLaneIds((current) => {
          const next = { ...current };
          delete next[sideLane.conversationId];
          return next;
        });
        setViewLaneIds((current) => {
          const next = { ...current };
          delete next[sideLane.conversationId];
          return next;
        });
        setLaneTrees((current) => {
          const next = { ...current };
          delete next[sideLane.conversationId];
          return next;
        });
      }
    } catch (closeError) {
      setCommandFeedback(target, null, readableError(closeError));
      return false;
    }
    dismissSideConversation();
    return true;
  }, [
    dismissSideConversation,
    runtimeController.snapshots,
    sideLane,
    statusFilter,
    waitForRuntimeRunToStop,
    workspaceId,
  ]);

  const openLaneInSide = useCallback(
    async (conversationId: string, laneId: string) => {
      if (sideLane?.mode === "temporary_conversation") {
        const closed = await closeSideConversation();
        if (!closed) return;
      } else {
        dismissSideConversation();
      }

      const requestVersion = sideRequestVersion.current + 1;
      sideRequestVersion.current = requestVersion;
      const target = { conversationId, laneId };
      setSideLane({
        ...target,
        mode: "branch_lane",
      });
      setSideConversationId(conversationId);
      setSideDraft("");
      setSideLoading(true);
      setCommandFeedback(target, "open-lane", null);
      try {
        const [legacy, runtime] = await Promise.all([
          chatApi.getConversation(conversationId),
          runtimeController.loadSnapshot(target),
        ]);
        if (sideRequestVersion.current !== requestVersion) return;
        applyRuntimeSnapshot(legacy, runtime, "side");
        if (runtime.runningRunId) {
          followRuntimeConversation(
            conversationId,
            runtime.lastEventSeq,
            laneId,
            "side",
          );
        }
        setCommandFeedback(target, null, null);
      } catch (loadError) {
        if (sideRequestVersion.current === requestVersion) {
          setCommandFeedback(target, null, readableError(loadError));
        }
      } finally {
        if (sideRequestVersion.current === requestVersion) {
          setSideLoading(false);
        }
      }
    },
    [
      applyRuntimeSnapshot,
      closeSideConversation,
      dismissSideConversation,
      followRuntimeConversation,
      sideLane,
    ],
  );

  const loadConversationList = useCallback(
    async (status: ConversationStatus, preferredId?: string) => {
      // 请求版本守卫：列表加载可能因 workspaceId/状态切换被并发触发，
      // 旧响应不得覆盖新状态（尤其刷新恢复时）。
      const requestVersion = conversationListVersion.current + 1;
      conversationListVersion.current = requestVersion;
      const isCurrent = () => conversationListVersion.current === requestVersion;
      setLoading(true);
      setError(null);
      try {
        let items = await chatApi.listConversations(status, undefined, workspaceId);
        if (!isCurrent()) return;
        if (status === "active" && items.length === 0 && workspaceCanCreate) {
          const created = await chatApi.createConversation(workspaceId!);
          if (!isCurrent()) return;
          items = [created];
        }
        setConversations(items);
        const target =
          items.find((item) => item.id === preferredId)?.id ?? items[0]?.id ?? null;
        if (isCurrent()) {
          if (target) await openConversation(target);
          else {
            setActiveConversationId(null);
            setLoading(false);
          }
        }
      } catch (loadError) {
        if (isCurrent()) {
          setError(readableError(loadError));
          setLoading(false);
        }
      }
    },
    [openConversation, workspaceId, workspaceCanCreate],
  );

  useEffect(() => {
    void chatApi.health().then(setHealth).catch(() => setHealth(null));
    void chatApi
      .capabilities()
      .then(setCapabilities)
      .catch(() => setCapabilities(null));
    void chatApi
      .listWorkspaces()
      .then(({ items, homePath }) => {
        setWorkspaces(items);
        setHomePath(homePath);
        // 刷新恢复上次会话：优先切到上次激活会话所在工作区；
        // 否则无已存上下文时默认落到第一个已绑定目录的工作区。
        const restoredConversation = readStoredActiveConversation();
        setWorkspaceId((current) => {
          if (current) return current;
          if (restoredConversation?.workspaceId) {
            const exists = items.some(
              (item) => item.id === restoredConversation.workspaceId,
            );
            if (exists) return restoredConversation.workspaceId;
          }
          const firstBound = items.find((item) => item.rootPath) ?? null;
          return firstBound?.id ?? null;
        });
      })
      .catch(() => setWorkspaces([]));
    void chatApi.listProviders().then(setProviders).catch(() => setProviders([]));
  }, []);

  const refreshCapabilities = useCallback(async () => {
    try {
      setCapabilities(await chatApi.capabilities());
    } catch {
      setCapabilities(null);
    }
  }, []);

  const refreshProviders = useCallback(async () => {
    try {
      setProviders(await chatApi.listProviders());
    } catch {
      setProviders([]);
    }
    try {
      setHealth(await chatApi.health());
    } catch {
      setHealth(null);
    }
    await refreshCapabilities();
  }, [refreshCapabilities]);

  useEffect(() => {
    // 刷新恢复上次会话：激活会话变化即持久化 id 与其工作区，供重载后恢复。
    // 仅在「本会话已主动打开过某个会话」后，关闭时才清除存储；否则首次挂载时
    // activeConversationId 为 null 会误清掉上次保存的恢复目标。
    if (!hasOpenedConversationRef.current) return;
    if (!activeConversationId) {
      try {
        globalThis.localStorage?.removeItem(ACTIVE_CONVERSATION_STORAGE_KEY);
      } catch {
        // 忽略存储异常。
      }
      return;
    }
    const snapshot = snapshots[activeConversationId];
    const workspace = snapshot?.conversation.workspaceId ?? null;
    try {
      globalThis.localStorage?.setItem(
        ACTIVE_CONVERSATION_STORAGE_KEY,
        JSON.stringify({ conversationId: activeConversationId, workspaceId: workspace }),
      );
    } catch {
      // 忽略存储异常。
    }
  }, [activeConversationId, snapshots]);

  useEffect(() => {
    // URL 深链（P0-4）：#/conversation/<id> 优先于本地恢复目标。
    const hashId = conversationIdFromHash();
    if (!hashId) return;
    const storedId = readStoredActiveConversation()?.conversationId ?? undefined;
    if (hashId === storedId) return; // 常规恢复路径已覆盖
    let cancelled = false;
    void chatApi
      .getConversation(hashId)
      .then((snapshot) => {
        if (cancelled) return;
        const workspaceId = snapshot.conversation.workspaceId ?? null;
        setWorkspaceId((current) => current ?? workspaceId);
        try {
          globalThis.localStorage?.setItem(
            ACTIVE_CONVERSATION_STORAGE_KEY,
            JSON.stringify({ conversationId: hashId, workspaceId }),
          );
        } catch {
          // 忽略存储异常。
        }
      })
      .catch(() => {
        // 无效/已删除会话：静默回退到常规恢复路径。
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // 刷新恢复上次会话：activeConversationId 尚未恢复时，用已持久化的会话 id 作为首选目标。
    const restored = activeConversationId
      ? activeConversationId
      : (readStoredActiveConversation()?.conversationId ?? undefined);
    void loadConversationList(statusFilter, restored);
    // active id 不作为重载触发；切换工作区时列表整体换防。
  }, [statusFilter, workspaceId, workspaceCanCreate]);

  useEffect(() => {
    // 主会话变化即同步 URL（replaceState，不产生历史项）；临时/遗留 ephemeral 不写。
    if (!activeConversationId) return;
    const snapshot = snapshots[activeConversationId];
    if (snapshot?.conversation.kind === "ephemeral") return;
    const next = `#/conversation/${encodeURIComponent(activeConversationId)}`;
    try {
      if ((globalThis.location?.hash ?? "") !== next) {
        globalThis.history?.replaceState(null, "", next);
      }
    } catch {
      // 忽略地址栏限制。
    }
  }, [activeConversationId, snapshots]);

  const activeConversationIdRef = useRef<string | null>(null);
  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);

  useEffect(() => {
    // 支持浏览器前进/后退与手动改地址打开其他会话。
    const onHashChange = () => {
      const id = conversationIdFromHash();
      if (!id || id === activeConversationIdRef.current) return;
      void openConversation(id);
    };
    globalThis.addEventListener?.("hashchange", onHashChange);
    return () => globalThis.removeEventListener?.("hashchange", onHashChange);
  }, [openConversation]);

  useEffect(
    () => () => {
      for (const controller of streams.current.values()) controller.abort();
      streams.current.clear();
    },
    [],
  );

  const activeSnapshot = activeConversationId
    ? snapshots[activeConversationId] ?? null
    : null;
  const latestTurn = activeSnapshot?.turns.at(-1);
  const latestLiveTurn = latestTurn ? liveTurns[latestTurn.turn.id] : undefined;
  const activeTurnStatus = latestLiveTurn?.status ?? latestTurn?.turn.status;
  const activeTurnStatusSet =
    activeTurnStatus && activeRuntimeStatuses.has(activeTurnStatus);
  const isGenerating = Boolean(activeTurnStatusSet);

  const sideSnapshot = sideConversationId
    ? (sideLane ? sideSnapshots[sideLane.laneId] : snapshots[sideConversationId]) ??
      null
    : null;
  const sideLatestTurn = sideSnapshot?.turns.at(-1);
  const sideLatestLiveTurn = sideLatestTurn
    ? liveTurns[sideLatestTurn.turn.id]
    : undefined;
  const sideTurnStatus = sideLatestLiveTurn?.status ?? sideLatestTurn?.turn.status;
  const sideIsGenerating = Boolean(
    sideTurnStatus && activeRuntimeStatuses.has(sideTurnStatus),
  );
  const activeRuntimeSnapshot = activeConversationId
    ? (runtimeController.snapshots[
        runtimeTargetKey({
          conversationId: activeConversationId,
          laneId: activeRuntimeLaneId,
        })
      ] ?? null)
    : null;
  const activeRuntimeConnection = activeConversationId
    ? (runtimeController.connections[activeConversationId] ?? null)
    : null;
  const activeRuntimeEvents = activeConversationId
    ? (runtimeController.events[activeConversationId] ?? [])
    : [];
  const sideRuntimeSnapshot = sideCommandTarget
    ? (runtimeController.snapshots[runtimeTargetKey(sideCommandTarget)] ?? null)
    : null;
  const sideRuntimeConnection = sideConversationId
    ? (runtimeController.connections[sideConversationId] ?? null)
    : null;

  // 当前激活 lane 是否正在运行：若是，composer 输入将作为「运行中 steer」注入，
  // 而不是开启新一轮对话（对齐 harness 的 steer/inject 交互）。
  const activeRuntimeRunId = activeRuntimeSnapshot?.runningRunId ?? null;
  const activeRunRunning = Boolean(
    activeRuntimeRunId &&
      activeRuntimeSnapshot?.runningLaneId === activeRuntimeLaneId,
  );
  const activeRunningRunId = activeRunRunning ? activeRuntimeRunId : null;

  const sendRuntimeV2Message = async (
    conversationId: string,
    content: string,
    laneId?: string | null,
    target: SnapshotTarget = "main",
  ) => {
    const requestVersion =
      target === "side"
        ? sideRequestVersion.current
        : primarySurfaceRequestVersion.current;
    const isCurrent = () =>
      target === "side"
        ? sideRequestVersion.current === requestVersion
        : primarySurfaceRequestVersion.current === requestVersion;
    const runtimeTarget = { conversationId, laneId: laneId ?? null };
    const before = await runtimeController.loadSnapshot(runtimeTarget);
    const result = await chatApi.createRuntimeV2Message(
      conversationId,
      content,
      laneId ?? before.activeLaneId,
      requestId(),
    );
    const legacy = await chatApi.getConversation(conversationId);
    const runtime = await runtimeController.loadSnapshot({
      conversationId,
      laneId: result.laneId,
    });
    if (!isCurrent()) return result;
    applyRuntimeSnapshot(legacy, runtime, target);
    followRuntimeConversation(
      conversationId,
      before.lastEventSeq,
      result.laneId,
      target,
    );
    return result;
  };

  const send = async () => {
    const content = draft.trim();
    if (!content || !activeConversationId) return;
    // 运行中且在当前 lane：把这条消息作为 steer 注入，允许打断/纠偏当前 Agent。
    if (activeRunRunning && activeRunningRunId) {
      const retainedDraft = draft;
      setDraft("");
      setCommandFeedback(primaryCommandTarget, "send", null);
      try {
        const result = await chatApi.steerRuntimeV2Run(
          activeRunningRunId,
          content,
        );
        setCommandFeedback(primaryCommandTarget, null, result.accepted ? null : "已提示，但运行未接受该指令");
      } catch (steerError) {
        setDraft(retainedDraft);
        setCommandFeedback(primaryCommandTarget, null, readableError(steerError));
      }
      return;
    }
    const retainedDraft = draft;
    if (isGenerating) return;
    setDraft("");
    setCommandFeedback(primaryCommandTarget, "send", null);
    try {
      await sendRuntimeV2Message(
        activeConversationId,
        content,
        viewLaneIds[activeConversationId] ?? mainLaneIds[activeConversationId],
      );
      setCommandFeedback(primaryCommandTarget, null, null);
    } catch (sendError) {
      setDraft(retainedDraft);
      setCommandFeedback(primaryCommandTarget, null, readableError(sendError));
    }
  };

  const sendSide = async () => {
    const content = sideDraft.trim();
    if (!content || !sideConversationId || sideIsGenerating) return;
    const retainedDraft = sideDraft;
    setSideDraft("");
    setCommandFeedback(sideCommandTarget, "send", null);
    try {
      await sendRuntimeV2Message(
        sideConversationId,
        content,
        sideLane?.conversationId === sideConversationId
          ? sideLane.laneId
          : mainLaneIds[sideConversationId],
        sideLane?.conversationId === sideConversationId ? "side" : "main",
      );
      setCommandFeedback(sideCommandTarget, null, null);
    } catch (sendError) {
      setSideDraft(retainedDraft);
      setCommandFeedback(sideCommandTarget, null, readableError(sendError));
    }
  };

  const uploadFile = async (file: File) => {
    if (!activeConversationId || isGenerating) return;
    setPendingAction("upload-file");
    setError(null);
    try {
      const uploaded = await chatApi.uploadFile(activeConversationId, file);
      setSnapshots((current) => {
        const snapshot = current[activeConversationId];
        if (!snapshot) return current;
        return {
          ...current,
          [activeConversationId]: {
            ...snapshot,
            files: [...snapshot.files, uploaded],
          },
        };
      });
    } catch (uploadError) {
      setError(readableError(uploadError));
    } finally {
      setPendingAction(null);
    }
  };

  const removeFile = async (fileId: string) => {
    if (!activeConversationId || isGenerating) return;
    setPendingAction(`remove-file:${fileId}`);
    setError(null);
    try {
      await chatApi.deleteFile(activeConversationId, fileId);
      setSnapshots((current) => {
        const snapshot = current[activeConversationId];
        if (!snapshot) return current;
        return {
          ...current,
          [activeConversationId]: {
            ...snapshot,
            files: snapshot.files.filter((item) => item.id !== fileId),
          },
        };
      });
    } catch (removeError) {
      setError(readableError(removeError));
    } finally {
      setPendingAction(null);
    }
  };

  const newConversation = async (targetWorkspaceId?: string) => {
    const targetId = targetWorkspaceId ?? workspaceId;
    setPendingAction("new");
    setError(null);
    const targetWorkspace = workspaces.find(
      (item) => item.id === targetId,
    ) ?? currentWorkspace;
    const canCreate = Boolean(targetWorkspace?.rootPath);
    if (!canCreate) {
      setError(
        targetWorkspace
          ? "该工作区尚未绑定本地目录，请先绑定目录后再新建对话。"
          : "请先选择并绑定一个工作区目录，再新建对话。",
      );
      setPendingAction(null);
      return;
    }
    if (targetWorkspaceId && targetWorkspaceId !== workspaceId) {
      setWorkspaceId(targetWorkspaceId);
    }
    try {
      const conversation = await chatApi.createConversation(targetId!);
      setStatusFilter("active");
      setConversations((current) => [
        conversation,
        ...current.filter((item) => item.id !== conversation.id),
      ]);
      await openConversation(conversation.id);
    } catch (createError) {
      setError(readableError(createError));
    } finally {
      setPendingAction(null);
    }
  };

  const createTemporaryConversation = async () => {
    if (!activeConversationId) return;
    if (sideLane?.mode === "temporary_conversation") {
      const closed = await closeSideConversation();
      if (!closed) return;
    } else if (sideLane) {
      dismissSideConversation();
    }
    setPendingAction("temporary-conversation");
    setError(null);
    try {
      const created = await chatApi.createRuntimeV2TemporaryConversation(
        activeConversationId,
        {},
      );
      const temporaryConversationId = created.conversation.id;
      const [legacy, runtime] = await Promise.all([
        chatApi.getConversation(temporaryConversationId),
        runtimeController.loadSnapshot({
          conversationId: temporaryConversationId,
          laneId: created.lane.id,
        }),
      ]);
      setMainLaneIds((current) => ({
        ...current,
        [temporaryConversationId]: created.lane.id,
      }));
      setViewLaneIds((current) => ({
        ...current,
        [temporaryConversationId]: created.lane.id,
      }));
      setLaneTrees((current) => ({
        ...current,
        [temporaryConversationId]: [created.lane],
      }));
      setSideLane({
        conversationId: temporaryConversationId,
        laneId: created.lane.id,
        mode: "temporary_conversation",
      });
      setSideConversationId(temporaryConversationId);
      setSideDraft("");
      applyRuntimeSnapshot(legacy, runtime, "side");
    } catch (branchError) {
      setError(readableError(branchError));
    } finally {
      setPendingAction(null);
    }
  };

  const refreshLaneTree = useCallback(async (conversationId: string) => {
    try {
      const laneList = await chatApi.listRuntimeV2Lanes(conversationId);
      setLaneTrees((current) => ({ ...current, [conversationId]: laneList.items }));
      const activeLaneId = laneList.activeLaneId;
      if (activeLaneId) {
        setMainLaneIds((current) => ({
          ...current,
          [conversationId]: activeLaneId,
        }));
      }
    } catch {
      // lane 树刷新失败不阻断交互。
    }
  }, []);

  const switchLane = useCallback(
    async (conversationId: string, laneId: string) => {
      const requestVersion = primarySurfaceRequestVersion.current + 1;
      primarySurfaceRequestVersion.current = requestVersion;
      setPendingAction("switch-lane");
      setError(null);
      try {
        const previousLaneId =
          viewLaneIds[conversationId] ?? mainLaneIds[conversationId] ?? null;
        if (previousLaneId && previousLaneId !== laneId) {
          unbindRuntimeTarget(
            { conversationId, laneId: previousLaneId },
            "main",
          );
        }
        setViewLaneIds((current) => ({
          ...current,
          [conversationId]: laneId,
        }));
        const [legacy, runtime] = await Promise.all([
          chatApi.getConversation(conversationId),
          runtimeController.loadSnapshot({ conversationId, laneId }),
        ]);
        if (primarySurfaceRequestVersion.current !== requestVersion) {
          unbindRuntimeTarget({ conversationId, laneId }, "main");
          return;
        }
        applyRuntimeSnapshot(legacy, runtime);
        followRuntimeConversation(conversationId, runtime.lastEventSeq, laneId);
      } catch (loadError) {
        if (primarySurfaceRequestVersion.current === requestVersion) {
          setError(readableError(loadError));
        }
      } finally {
        if (primarySurfaceRequestVersion.current === requestVersion) {
          setPendingAction(null);
        }
      }
    },
    [
      applyRuntimeSnapshot,
      followRuntimeConversation,
      mainLaneIds,
      runtimeController.loadSnapshot,
      unbindRuntimeTarget,
      viewLaneIds,
    ],
  );

  const forkLane = useCallback(
    async (
      conversationId: string,
      sourceLaneId: string | null | undefined,
      forkTurnId?: string,
    ) => {
      setPendingAction("fork-lane");
      setError(null);
      try {
        if (!forkTurnId) {
          throw new Error("请从一条已完成的回答创建分支。");
        }
        const turnSnapshot = snapshots[conversationId]?.turns.find(
          (item) => item.turn.id === forkTurnId,
        );
        const baseEntryId = turnSnapshot?.responseVariants.find(
          (item) => item.variant.id === turnSnapshot.turn.activeResponseVariantId,
        )?.assistantMessage.id;
        if (!baseEntryId || turnSnapshot?.turn.status !== "completed") {
          throw new Error("只能从完整回答结束处创建分支。");
        }
        const resolvedSourceLaneId =
          sourceLaneId ?? viewLaneIds[conversationId] ?? mainLaneIds[conversationId];
        if (!resolvedSourceLaneId) {
          throw new Error("当前会话暂不支持创建分支，请确认会话已加载主线。");
        }
        const created = await chatApi.createRuntimeV2Lane(conversationId, {
          sourceLaneId: resolvedSourceLaneId,
          baseEntryId,
        });
        await refreshLaneTree(conversationId);
        await openLaneInSide(conversationId, created.lane.id);
      } catch (forkError) {
        setError(readableError(forkError));
      } finally {
        setPendingAction(null);
      }
    },
    [mainLaneIds, openLaneInSide, refreshLaneTree, snapshots, viewLaneIds],
  );

  const promoteLane = useCallback(
    async (conversationId: string, laneId: string) => {
      setPendingAction("promote-lane");
      setError(null);
      try {
        await chatApi.promoteRuntimeV2Lane(laneId);
        if (sideLane?.conversationId === conversationId && sideLane.laneId === laneId) {
          dismissSideConversation();
        }
        await refreshLaneTree(conversationId);
        await loadConversation(conversationId);
      } catch (promoteError) {
        setError(readableError(promoteError));
      } finally {
        setPendingAction(null);
      }
    },
    [dismissSideConversation, loadConversation, refreshLaneTree, sideLane],
  );

  const renameLane = useCallback(
    async (conversationId: string, laneId: string, displayName: string | null) => {
      setPendingAction("rename-lane");
      setError(null);
      try {
        await chatApi.renameRuntimeV2Lane(laneId, displayName);
        await refreshLaneTree(conversationId);
      } catch (renameError) {
        setError(readableError(renameError));
      } finally {
        setPendingAction(null);
      }
    },
    [refreshLaneTree],
  );

  const setLaneArchived = useCallback(
    async (
      conversationId: string,
      laneId: string,
      archived: boolean,
      includeArchived = false,
    ) => {
      setPendingAction(archived ? "archive-lane" : "restore-lane");
      setError(null);
      try {
        const updated = archived
          ? await chatApi.archiveRuntimeV2Lane(laneId)
          : await chatApi.restoreRuntimeV2Lane(laneId);
        const laneList = await chatApi.listRuntimeV2Lanes(
          conversationId,
          includeArchived,
        );
        setLaneTrees((current) => ({
          ...current,
          [conversationId]: laneList.items,
        }));
        const affectedLaneIds = new Set(updated.items.map((lane) => lane.id));
        if (
          archived &&
          sideLane?.conversationId === conversationId &&
          affectedLaneIds.has(sideLane.laneId)
        ) {
          dismissSideConversation();
        }
        const visibleLaneId = viewLaneIds[conversationId];
        if (
          archived &&
          visibleLaneId &&
          updated.items.some((lane) => lane.id === visibleLaneId)
        ) {
          const nextLaneId = laneList.activeLaneId;
          if (nextLaneId) {
            setViewLaneIds((current) => ({
              ...current,
              [conversationId]: nextLaneId,
            }));
            const [legacy, runtime] = await Promise.all([
              chatApi.getConversation(conversationId),
              runtimeController.loadSnapshot({
                conversationId,
                laneId: nextLaneId,
              }),
            ]);
            applyRuntimeSnapshot(legacy, runtime);
          }
        }
      } catch (archiveError) {
        setError(readableError(archiveError));
      } finally {
        setPendingAction(null);
      }
    },
    [applyRuntimeSnapshot, dismissSideConversation, sideLane, viewLaneIds],
  );

  const setArchivedLanesVisible = useCallback(
    async (conversationId: string, visible: boolean) => {
      setError(null);
      try {
        const laneList = await chatApi.listRuntimeV2Lanes(
          conversationId,
          visible,
        );
        setLaneTrees((current) => ({
          ...current,
          [conversationId]: laneList.items,
        }));
      } catch (loadError) {
        setError(readableError(loadError));
      }
    },
    [],
  );

  const promoteConversation = async (
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    const feedbackTarget =
      surface === "side" ? sideCommandTarget : primaryCommandTarget;
    setCommandFeedback(feedbackTarget, "promote", null);
    try {
      if (
        sideLane?.conversationId === targetId &&
        sideLane.mode === "temporary_conversation"
      ) {
        const promoted =
          await chatApi.promoteRuntimeV2TemporaryConversation(targetId);
        dismissSideConversation();
        try {
          const items = await chatApi.listConversations(
            statusFilter,
            undefined,
            workspaceId,
          );
          setConversations(items);
        } catch {
          setConversations((current) =>
            statusFilter === "active"
              ? [
                  promoted.conversation,
                  ...current.filter(
                    (item) => item.id !== promoted.conversation.id,
                  ),
                ]
              : current.filter(
                  (item) => item.id !== promoted.conversation.id,
                ),
          );
        }
        return;
      }

      if (sideLane?.conversationId === targetId) {
        const promoted = await chatApi.promoteRuntimeV2Lane(sideLane.laneId);
        setMainLaneIds((current) => ({
          ...current,
          [targetId]: promoted.activeLaneId,
        }));
        dismissSideConversation();
        await refreshLaneTree(targetId);
        await loadConversation(targetId);
        return;
      }

      const promoted =
        await chatApi.promoteRuntimeV2TemporaryConversation(targetId);
      setConversations((current) =>
        current.map((item) =>
          item.id === promoted.conversation.id ? promoted.conversation : item,
        ),
      );
      setSnapshots((current) => {
        const snapshot = current[promoted.conversation.id];
        return snapshot
          ? {
              ...current,
              [promoted.conversation.id]: {
                ...snapshot,
                conversation: promoted.conversation,
              },
            }
          : current;
      });
      if (targetId === sideConversationId) {
        dismissSideConversation();
        try {
          const items = await chatApi.listConversations(
            statusFilter,
            undefined,
            workspaceId,
          );
          setConversations(items);
        } catch {
          setConversations((current) =>
            statusFilter === "active"
              ? [
                  promoted.conversation,
                  ...current.filter(
                    (item) => item.id !== promoted.conversation.id,
                  ),
                ]
              : current.filter(
                  (item) => item.id !== promoted.conversation.id,
                ),
          );
        }
      }
      setCommandFeedback(feedbackTarget, null, null);
    } catch (promoteError) {
      setCommandFeedback(feedbackTarget, null, readableError(promoteError));
    }
  };

  const renameConversation = async (title: string, conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("rename");
    try {
      const updated = await chatApi.patchConversation(targetId, { title });
      setConversations((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setSnapshots((current) => {
        const snapshot = current[updated.id];
        return snapshot
          ? { ...current, [updated.id]: { ...snapshot, conversation: updated } }
          : current;
      });
    } catch (renameError) {
      setError(readableError(renameError));
    } finally {
      setPendingAction(null);
    }
  };

  const changeConversationModel = async (
    providerProfileId: string | null,
    modelOverride: string | null,
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    const feedbackTarget =
      surface === "side" ? sideCommandTarget : primaryCommandTarget;
    setCommandFeedback(feedbackTarget, "provider", null);
    try {
      const updated = await chatApi.patchConversation(targetId, {
        providerProfileId,
        modelOverride,
      });
      setSnapshots((current) => {
        const snapshot = current[updated.id];
        return snapshot
          ? { ...current, [updated.id]: { ...snapshot, conversation: updated } }
          : current;
      });
      setCommandFeedback(feedbackTarget, null, null);
    } catch (modelError) {
      setCommandFeedback(feedbackTarget, null, readableError(modelError));
    }
  };

  const changeConversationStatus = async (
    status: ConversationStatus,
    conversationId?: string,
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("status");
    try {
      const updated = await chatApi.patchConversation(targetId, { status });
      const nextFilter = status === "active" ? "active" : statusFilter;
      setStatusFilter(nextFilter);
      await loadConversationList(
        nextFilter,
        status === "active" ? updated.id : undefined,
      );
    } catch (statusError) {
      setError(readableError(statusError));
    } finally {
      setPendingAction(null);
    }
  };

  const deleteConversation = async (conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("delete");
    try {
      await chatApi.deleteConversation(targetId);
      setSnapshots((current) => {
        const next = { ...current };
        delete next[targetId];
        return next;
      });
      if (targetId === activeConversationId) setActiveConversationId(null);
      if (targetId === sideConversationId) {
        setSideConversationId(null);
      } else if (
        sideConversationId &&
        snapshots[sideConversationId]?.conversation.parentConversationId ===
          targetId
      ) {
        setSideConversationId(null);
      }
      await loadConversationList(statusFilter);
    } catch (deleteError) {
      setError(readableError(deleteError));
    } finally {
      setPendingAction(null);
    }
  };

  const cancel = async (
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    const feedbackTarget =
      surface === "side" ? sideCommandTarget : primaryCommandTarget;
    const targetSnapshot =
      surface === "side"
        ? sideSnapshot
        : targetId
          ? snapshots[targetId]
          : null;
    const targetTurn = targetSnapshot?.turns.at(-1) ?? latestTurn;
    if (
      !targetTurn ||
      !targetId ||
      targetTurn.turn.conversationId !== targetId
    ) {
      return;
    }
    setCommandFeedback(feedbackTarget, "cancel", null);
    try {
      await chatApi.cancelRuntimeV2Run(targetTurn.turn.id);
      setCommandFeedback(feedbackTarget, null, null);
    } catch (cancelError) {
      setCommandFeedback(feedbackTarget, null, readableError(cancelError));
    }
  };

  const cancelRuntimeRun = async (
    runId: string,
    surface: SnapshotTarget = "main",
  ) => {
    const target = surface === "side" ? sideCommandTarget : primaryCommandTarget;
    if (!target) return;
    setCommandFeedback(target, "cancel-running", null);
    try {
      await chatApi.cancelRuntimeV2Run(runId);
      const [legacy, runtime] = await Promise.all([
        chatApi.getConversation(target.conversationId),
        runtimeController.loadSnapshot(target),
      ]);
      applyRuntimeSnapshot(legacy, runtime, surface);
      if (runtime.runningRunId) {
        followRuntimeConversation(
          target.conversationId,
          runtime.lastEventSeq,
          target.laneId,
          surface,
        );
      }
      setCommandFeedback(target, null, null);
    } catch (cancelError) {
      setCommandFeedback(target, null, readableError(cancelError));
    }
  };

  // C2：卡住横幅的「换一条路径」——把纠偏指令注入运行中的 steer 通道。
  const steerRuntimeRun = async (
    runId: string,
    content: string,
    surface: SnapshotTarget = "main",
  ) => {
    const target = surface === "side" ? sideCommandTarget : primaryCommandTarget;
    if (!target) return;
    setCommandFeedback(target, "steer-running", null);
    try {
      const result = await chatApi.steerRuntimeV2Run(runId, content);
      setCommandFeedback(
        target,
        null,
        result.accepted ? null : "已提示，但运行未接受该指令",
      );
    } catch (steerError) {
      setCommandFeedback(target, null, readableError(steerError));
    }
  };

  const resolveApproval = async (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny" | "modify",
    args?: Record<string, unknown>,
    surface: SnapshotTarget = "main",
  ) => {
    const feedbackTarget =
      surface === "side" ? sideCommandTarget : primaryCommandTarget;
    setCommandFeedback(feedbackTarget, `approval:${approvalId}`, null);
    try {
      await chatApi.resolveRuntimeV2Approval(approvalId, decision, args);
      setLiveTurns((current) => {
        const live = current[turnId];
        if (!live || live.pendingApproval?.id !== approvalId) return current;
        return {
          ...current,
          [turnId]: { ...live, pendingApproval: undefined },
        };
      });
      setCommandFeedback(feedbackTarget, null, null);
    } catch (approvalError) {
      setCommandFeedback(feedbackTarget, null, readableError(approvalError));
    }
  };

  const resolveRuntimeRecovery = async (
    runId: string,
    action: "retry" | "mark_failed",
    surface: SnapshotTarget = "main",
  ) => {
    const target = surface === "side" ? sideCommandTarget : primaryCommandTarget;
    if (!target) return;
    const requestVersion =
      surface === "side"
        ? sideRequestVersion.current
        : primarySurfaceRequestVersion.current;
    const isCurrent = () =>
      surface === "side"
        ? sideRequestVersion.current === requestVersion
        : primarySurfaceRequestVersion.current === requestVersion;
    setCommandFeedback(target, `recovery:${action}`, null);
    try {
      const result = await chatApi.resolveRuntimeV2Recovery(runId, action);
      const runtimeTarget = {
        conversationId: target.conversationId,
        laneId: result.laneId ?? target.laneId,
      };
      const [legacy, runtime] = await Promise.all([
        chatApi.getConversation(target.conversationId),
        runtimeController.loadSnapshot(runtimeTarget),
      ]);
      if (!isCurrent()) {
        setCommandFeedback(target, null, null);
        return;
      }
      applyRuntimeSnapshot(legacy, runtime, surface);
      if (result.newRunId || runtime.runState) {
        followRuntimeConversation(
          target.conversationId,
          runtime.lastEventSeq,
          runtimeTarget.laneId,
          surface,
        );
      }
      setCommandFeedback(target, null, null);
    } catch (recoveryError) {
      if (isCurrent()) {
        setCommandFeedback(target, null, readableError(recoveryError));
      }
    }
  };

  const runRuntimeV2Command = async (
    action: "retry" | "regenerate",
    conversationId: string,
    runId: string,
    laneId?: string | null,
    surface: SnapshotTarget = "main",
  ) => {
    const feedbackTarget =
      surface === "side" ? sideCommandTarget : primaryCommandTarget;
    const requestVersion =
      surface === "side"
        ? sideRequestVersion.current
        : primarySurfaceRequestVersion.current;
    const isCurrent = () =>
      surface === "side"
        ? sideRequestVersion.current === requestVersion
        : primarySurfaceRequestVersion.current === requestVersion;
    setCommandFeedback(feedbackTarget, action, null);
    try {
      const target = { conversationId, laneId: laneId ?? null };
      const before = await runtimeController.loadSnapshot(target);
      await chatApi.regenerateRuntimeV2Run(runId);
      const runtime = await runtimeController.loadSnapshot(target);
      const legacy = await chatApi.getConversation(runtime.conversationId);
      if (!isCurrent()) {
        setCommandFeedback(feedbackTarget, null, null);
        return;
      }
      applyRuntimeSnapshot(legacy, runtime, surface);
      followRuntimeConversation(
        runtime.conversationId,
        before.lastEventSeq,
        target.laneId,
        surface,
      );
      setCommandFeedback(feedbackTarget, null, null);
    } catch (commandError) {
      if (isCurrent()) {
        setCommandFeedback(feedbackTarget, null, readableError(commandError));
      }
    }
  };

  const retry = async (
    turnId: string,
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    const isSide = surface === "side";
    const laneId = isSide
      ? sideLane?.laneId
      : targetId
        ? (viewLaneIds[targetId] ?? mainLaneIds[targetId])
        : undefined;
    await runRuntimeV2Command("retry", targetId!, turnId, laneId, surface);
  };

  const regenerate = async (
    turnId: string,
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    const isSide = surface === "side";
    const laneId = isSide
      ? sideLane?.laneId
      : targetId
        ? (viewLaneIds[targetId] ?? mainLaneIds[targetId])
        : undefined;
    await runRuntimeV2Command("regenerate", targetId!, turnId, laneId, surface);
  };

  const resend = async (
    turnId: string,
    content: string,
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    const isSide = surface === "side";
    const laneId = isSide
      ? sideLane?.laneId
      : (viewLaneIds[targetId] ?? mainLaneIds[targetId]);
    const feedbackTarget = isSide ? sideCommandTarget : primaryCommandTarget;
    const requestVersion = isSide
      ? sideRequestVersion.current
      : primarySurfaceRequestVersion.current;
    const isCurrent = () =>
      isSide
        ? sideRequestVersion.current === requestVersion
        : primarySurfaceRequestVersion.current === requestVersion;
    setCommandFeedback(feedbackTarget, "resend", null);
    try {
      const runtimeTarget = { conversationId: targetId, laneId: laneId ?? null };
      const before = await runtimeController.loadSnapshot(runtimeTarget);
      await chatApi.resendRuntimeV2Run(turnId, content);
      if (!isCurrent()) {
        setCommandFeedback(feedbackTarget, null, null);
        return;
      }
      // 202 后立即记录改写展示（即使后续 snapshot 刷新异常也保留编辑态）
      setEditedUserMessages((current) => ({ ...current, [turnId]: content }));
      const runtime = await runtimeController.loadSnapshot(runtimeTarget);
      const legacy = await chatApi.getConversation(runtime.conversationId);
      if (!isCurrent()) {
        setCommandFeedback(feedbackTarget, null, null);
        return;
      }
      applyRuntimeSnapshot(legacy, runtime, surface);
      followRuntimeConversation(
        runtime.conversationId,
        before.lastEventSeq,
        runtimeTarget.laneId,
        surface,
      );
      setCommandFeedback(feedbackTarget, null, null);
    } catch (resendError) {
      if (isCurrent()) {
        setCommandFeedback(feedbackTarget, null, readableError(resendError));
      }
    }
  };

  const selectVariant = async (
    turnId: string,
    variantId: string,
    conversationId?: string,
    surface: SnapshotTarget = "main",
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    const isSide = surface === "side";
    const feedbackTarget = isSide ? sideCommandTarget : primaryCommandTarget;
    setCommandFeedback(feedbackTarget, "select", null);
    try {
      await chatApi.selectRuntimeV2RunVariant(variantId);
      const laneId = isSide
        ? sideLane?.laneId
        : (viewLaneIds[targetId] ?? mainLaneIds[targetId]);
      const runtime = await runtimeController.loadSnapshot({
        conversationId: targetId,
        laneId: laneId ?? null,
      });
      const legacy = await chatApi.getConversation(targetId);
      applyRuntimeSnapshot(legacy, runtime, isSide ? "side" : "main");
      setCommandFeedback(feedbackTarget, null, null);
    } catch (selectionError) {
      setCommandFeedback(feedbackTarget, null, readableError(selectionError));
    }
  };

  const visibleConversations = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return conversations;
    return conversations.filter((item) => item.title.toLocaleLowerCase().includes(query));
  }, [conversations, search]);

  const selectWorkspace = (nextWorkspaceId: string | null) => {
    setWorkspaceId(nextWorkspaceId);
    try {
      globalThis.localStorage?.setItem(
        WORKSPACE_STORAGE_KEY,
        nextWorkspaceId ?? "general",
      );
    } catch {
      // 存储不可用时仅内存态，忽略。
    }
  };

  const createWorkspace = async (name: string, rootPath?: string | null) => {
    const workspace = await chatApi.createWorkspace(name, rootPath);
    setWorkspaces((current) => [...current, workspace]);
    selectWorkspace(workspace.id);
    return workspace;
  };

  const refreshWorkspaces = async () => {
    try {
      const { items, homePath } = await chatApi.listWorkspaces();
      setWorkspaces(items);
      setHomePath(homePath);
    } catch {
      // 列表刷新失败保持现有状态，忽略。
    }
  };

  const deleteWorkspace = async (workspaceIdToDelete: string) => {
    await chatApi.deleteWorkspace(workspaceIdToDelete);
    setWorkspaces((current) =>
      current.filter((item) => item.id !== workspaceIdToDelete),
    );
    if (workspaceId === workspaceIdToDelete) {
      selectWorkspace(null);
    }
  };

  return {
    capabilities,
    activeConversationId,
    activeSnapshot,
    activeRuntimeConnection,
    activeRuntimeEvents,
    activeRuntimeSnapshot,
    createWorkspace,
    currentWorkspace,
    deleteWorkspace,
    homePath,
    refreshWorkspaces,
    selectWorkspace,
    workspaceCanCreate,
    workspaceId,
    workspaces,
    changeConversationStatus,
    closeSideConversation,
    conversations: visibleConversations,
    createTemporaryConversation,
    focusTemporaryConversation,
    openLaneInSide,
    openSideConversation,
    promoteConversation,
    sendSide,
    setSideDraft,
    sideConversationId,
    sideMode: sideLane?.mode ?? null,
    sideDraft,
    sideIsGenerating,
    sideLoading,
    sideSnapshot,
    sideRuntimeConnection,
    sideRuntimeSnapshot,
    cancel,
    cancelRuntimeRun,
    steerRuntimeRun,
    deleteConversation,
    draft,
    error: primaryCommandState?.error ?? error,
    sideError: sideCommandState?.error ?? null,
    health,
    isGenerating,
    activeRunRunning,
    liveTurns,
    loading,
    newConversation,
    openConversation,
    editedUserMessages,
    pendingAction: primaryCommandState?.pendingAction ?? pendingAction,
    sidePendingAction: sideCommandState?.pendingAction ?? null,
    providers,
    regenerate,
    resend,
    removeFile,
    resolveApproval,
    resolveRuntimeRecovery,
    renameConversation,
    retry,
    refreshCapabilities,
    refreshProviders,
    search,
    selectVariant,
    changeConversationModel,
    send,
    setDraft,
    dismissPrimaryError,
    dismissSideError,
    setSearch,
    setStatusFilter,
    statusFilter,
    laneTrees,
    mainLaneIds,
    viewLaneIds,
    forkLane,
    switchLane,
    promoteLane,
    renameLane,
    setLaneArchived,
    setArchivedLanesVisible,
    uploadFile,
  };
}
