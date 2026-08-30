import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiClientError,
  chatApi,
  streamRuntimeV2Events,
  streamTurnEvents,
} from "./api";
import type {
  CapabilitySnapshot,
  CompactTurnSnapshot,
  ActivityStatus,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  LiveTurn,
  ProviderProfile,
  RuntimeV2ConversationRuntimeStatus,
  RuntimeV2Entry,
  RuntimeV2Lane,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeEvent,
  Message,
  TurnStatus,
  TurnCommandResponse,
  Workspace,
} from "./apiTypes";

const WORKSPACE_STORAGE_KEY = "endless-task.workspace";

const readStoredWorkspace = (): string | null => {
  try {
    const value = globalThis.localStorage?.getItem(WORKSPACE_STORAGE_KEY);
    return value && value !== "general" ? value : null;
  } catch {
    return null;
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

const requestId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;

const readableError = (error: unknown) => {
  if (error instanceof ApiClientError) return error.message;
  if (error instanceof Error && error.name === "AbortError") return "";
  return "无法连接本地服务，请确认 API 已启动。";
};

const liveFromSnapshot = (snapshot: CompactTurnSnapshot): LiveTurn => ({
  turnId: snapshot.turnId,
  responseVariantId: snapshot.activeResponseVariantId,
  status: snapshot.turnStatus,
  content: snapshot.content,
  lastSequence: snapshot.lastSequence,
  error: snapshot.error,
  pendingApproval: snapshot.pendingApproval,
  activities: snapshot.activities,
});

const runtimeStatusToTurnStatus = (status: string): TurnStatus =>
  status === "completed" ||
  status === "failed" ||
  status === "cancelled" ||
  status === "running"
    ? status
    : "running";

const runtimeEntryStatusToTurnStatus = (status: string): TurnStatus =>
  status === "failed" || status === "cancelled" ? status : "completed";

const entryMessage = (
  entry: RuntimeV2Entry,
  role: "user" | "assistant",
  turnId: string,
): Message => ({
  id: entry.id,
  conversationId: "",
  turnId,
  role,
  content: entry.data.content ?? "",
  createdAt: entry.createdAt,
  updatedAt: entry.createdAt,
});

type SnapshotTarget = "main" | "side";

type SideLaneTarget = {
  conversationId: string;
  laneId: string;
  mode: "temporary_conversation" | "branch_lane";
};

const runtimeSnapshotToConversationSnapshot = (
  legacy: ConversationSnapshot,
  runtime: RuntimeV2Snapshot,
): ConversationSnapshot => {
  const turns: ConversationSnapshot["turns"] = [];
  let draft: {
    user: RuntimeV2Entry;
    variants: RuntimeV2Entry[];
  } | null = null;

  const flushDraft = (status: TurnStatus = "completed") => {
    if (!draft) return;
    const currentDraft = draft;
    const firstAssistant = currentDraft.variants[0];
    const turnId =
      firstAssistant?.sourceRunId ??
      runtime.activeRunId ??
      `${currentDraft.user.id}:turn`;
    const variantStatus = firstAssistant
      ? runtimeEntryStatusToTurnStatus(firstAssistant.status)
      : status;
    turns.push({
      turn: {
        id: turnId,
        conversationId: legacy.conversation.id,
        ordinal: turns.length + 1,
        userMessageId: currentDraft.user.id,
        activeResponseVariantId: firstAssistant?.sourceRunId ?? turnId,
        status: variantStatus,
        createdAt: currentDraft.user.createdAt,
        startedAt: currentDraft.user.createdAt,
        finishedAt: firstAssistant?.createdAt ?? null,
      },
      userMessage: entryMessage(currentDraft.user, "user", turnId),
      activeResponseVariantId: firstAssistant?.sourceRunId ?? turnId,
      responseVariants: currentDraft.variants.map((assistant, index) => ({
        variant: {
          id: assistant.sourceRunId ?? `${turnId}:variant:${index}`,
          turnId,
          assistantMessageId: assistant.id,
          index,
          operation: "create",
          status: runtimeEntryStatusToTurnStatus(assistant.status),
          provider: null,
          model: null,
          finishReason:
            assistant.status === "cancelled"
              ? "cancelled"
              : assistant.status === "failed"
                ? "error"
                : "stop",
          errorCode: null,
          inputTokens: null,
          outputTokens: null,
          createdAt: assistant.createdAt,
          startedAt: null,
          finishedAt: assistant.createdAt,
        },
        assistantMessage: entryMessage(assistant, "assistant", turnId),
      })),
      activities: [],
    });
    if (!currentDraft.variants.length && status !== "completed") {
      turns.at(-1)!.responseVariants.push({
        variant: {
          id: turnId,
          turnId,
          assistantMessageId: `${turnId}:pending`,
          index: 0,
          operation: "create",
          status,
          provider: null,
          model: null,
          finishReason: null,
          errorCode: null,
          inputTokens: null,
          outputTokens: null,
          createdAt: currentDraft.user.createdAt,
          startedAt: currentDraft.user.createdAt,
          finishedAt: null,
        },
        assistantMessage: {
          id: `${turnId}:pending`,
          conversationId: legacy.conversation.id,
          turnId,
          role: "assistant",
          content: "",
          createdAt: currentDraft.user.createdAt,
          updatedAt: currentDraft.user.createdAt,
        },
      });
    }
    draft = null;
  };

  for (const entry of runtime.entries) {
    if (entry.type === "user_message") {
      flushDraft();
      draft = { user: entry, variants: [] };
      continue;
    }
    if (entry.type === "assistant_message" && draft) {
      draft.variants.push(entry);
    }
  }

  if (draft) {
    flushDraft(
      runtime.runState ? runtimeStatusToTurnStatus(runtime.runState.status) : "completed",
    );
  }

  return {
    ...legacy,
    conversation: {
      ...legacy.conversation,
      nextTurnOrdinal: turns.length + 1,
    },
    turns,
  };
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
    metadata: { toolName: approval.toolName },
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
  const [sideConversationId, setSideConversationId] = useState<string | null>(null);
  const [sideLane, setSideLane] = useState<SideLaneTarget | null>(null);
  const [sideDraft, setSideDraft] = useState("");
  const [sideLoading, setSideLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ConversationStatus>("active");
  const [search, setSearch] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(
    readStoredWorkspace,
  );
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null);
  const [runtimeStatuses, setRuntimeStatuses] = useState<
    Record<string, RuntimeV2ConversationRuntimeStatus>
  >({});
  const [mainLaneIds, setMainLaneIds] = useState<Record<string, string>>({});
  const [viewLaneIds, setViewLaneIds] = useState<Record<string, string>>({});
  const [laneTrees, setLaneTrees] = useState<Record<string, RuntimeV2Lane[]>>({});
  const streams = useRef(new Map<string, AbortController>());

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
          metadata: {},
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
        const activity = {
          id: toolId,
          status: activityStatus,
          message: `工具执行 ${toolId.split(":").pop() ?? toolId}：${rawStatus}`,
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
      const streamKey = `v2:${conversationId}:${laneId ?? "active"}:${target}`;
      if (streams.current.has(streamKey)) return;
      const controller = new AbortController();
      streams.current.set(streamKey, controller);

      void (async () => {
        let cursor = initialSequence;
        try {
          while (!controller.signal.aborted) {
            try {
              await streamRuntimeV2Events({
                conversationId,
                laneId,
                afterSequence: cursor,
                signal: controller.signal,
                onSnapshot: (snapshot) => {
                  cursor = Math.max(cursor, snapshot.lastEventSeq);
                  void chatApi
                    .getConversation(conversationId)
                    .then((legacy) =>
                      applyRuntimeSnapshot(legacy, snapshot, target),
                    )
                    .catch(() => undefined);
                },
                onProductEvent: (event) => {
                  cursor = Math.max(cursor, event.eventSeq);
                  applyRuntimeProductEvent(event);
                },
              });
              const runtime = await chatApi.getRuntimeV2Snapshot(
                conversationId,
                laneId,
              );
              const legacy = await chatApi.getConversation(conversationId);
              applyRuntimeSnapshot(legacy, runtime, target);
              cursor = Math.max(cursor, runtime.lastEventSeq);
              if (!runtime.runState || terminalStatuses.has(runtime.runState.status)) {
                break;
              }
            } catch (streamError) {
              if (controller.signal.aborted) return;
              setError(readableError(streamError));
            }
            await new Promise((resolve) => globalThis.setTimeout(resolve, 800));
          }
        } finally {
          streams.current.delete(streamKey);
        }
      })();
    },
    [applyRuntimeProductEvent, applyRuntimeSnapshot],
  );

  const applyEvent = useCallback((event: RuntimeEvent) => {
    setLiveTurns((current) => {
      const previous = current[event.turnId];
      if (previous && event.sequence <= previous.lastSequence) return current;
      const variantChanged =
        event.responseVariantId && event.responseVariantId !== previous?.responseVariantId;
      const next: LiveTurn = {
        turnId: event.turnId,
        responseVariantId:
          event.responseVariantId ?? previous?.responseVariantId ?? "",
        status: previous?.status ?? "created",
        content: variantChanged ? "" : (previous?.content ?? ""),
        lastSequence: event.sequence,
        error: variantChanged ? undefined : previous?.error,
        pendingApproval: variantChanged ? undefined : previous?.pendingApproval,
        activities: variantChanged ? [] : (previous?.activities ?? []),
      };

      if (event.type === "turn.started") next.status = "running";
      if (event.type === "message.started") next.content = "";
      if (event.type === "message.delta") next.content += event.data.delta ?? "";
      if (event.type === "message.completed") next.content = event.data.content ?? next.content;
      if (
        event.type === "approval.requested" &&
        event.data.approvalId &&
        event.data.toolCallId &&
        event.data.summary &&
        event.data.reason
      ) {
        next.pendingApproval = {
          id: event.data.approvalId,
          toolCallId: event.data.toolCallId,
          summary: event.data.summary,
          reason: event.data.reason,
          status: "pending",
          createdAt: event.data.createdAt ?? event.occurredAt,
          resolvedAt: null,
          metadata: event.data.metadata ?? {},
        };
      }
      if (
        event.type === "approval.resolved" &&
        next.pendingApproval?.id === event.data.approvalId
      ) {
        next.pendingApproval = undefined;
      }
      if (
        event.type.startsWith("activity.") &&
        event.data.activityId &&
        event.data.status &&
        event.data.message
      ) {
        const previousActivity = next.activities.find(
          (item) => item.id === event.data.activityId,
        );
        const updatedActivity = {
          id: event.data.activityId,
          status: event.data.status as ActivityStatus,
          message: event.data.message,
          startedAt: previousActivity?.startedAt ?? event.occurredAt,
          updatedAt: event.occurredAt,
        };
        next.activities = previousActivity
          ? next.activities.map((item) =>
              item.id === updatedActivity.id ? updatedActivity : item,
            )
          : [...next.activities, updatedActivity];
      }
      if (event.type === "turn.completed") {
        next.status = "completed";
        next.pendingApproval = undefined;
      }
      if (event.type === "turn.failed") {
        next.status = "failed";
        next.content = event.data.partialContent ?? next.content;
        next.error = event.data.error;
        next.pendingApproval = undefined;
      }
      if (event.type === "turn.cancelled") {
        next.status = "cancelled";
        next.content = event.data.partialContent ?? next.content;
        next.pendingApproval = undefined;
      }
      return { ...current, [event.turnId]: next };
    });
  }, []);

  const followTurn = useCallback(
    (turnId: string, conversationId: string, initialSequence: number) => {
      if (streams.current.has(turnId)) return;
      const controller = new AbortController();
      streams.current.set(turnId, controller);

      void (async () => {
        let cursor = initialSequence;
        try {
          while (!controller.signal.aborted) {
            try {
              await streamTurnEvents({
                turnId,
                afterSequence: cursor,
                signal: controller.signal,
                onEvent: (event) => {
                  cursor = Math.max(cursor, event.sequence);
                  applyEvent(event);
                },
              });
              const compact = await chatApi.getTurn(turnId);
              cursor = Math.max(cursor, compact.lastSequence);
              setLiveTurns((current) => ({
                ...current,
                [turnId]: liveFromSnapshot(compact),
              }));
              if (terminalStatuses.has(compact.turnStatus)) break;
            } catch (streamError) {
              if (controller.signal.aborted) return;
              setError(readableError(streamError));
            }
            await new Promise((resolve) => globalThis.setTimeout(resolve, 800));
          }
        } finally {
          streams.current.delete(turnId);
          if (!controller.signal.aborted) {
            try {
              const snapshot = await chatApi.getConversation(conversationId);
              setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
              setConversations((current) =>
                current.map((item) =>
                  item.id === conversationId ? snapshot.conversation : item,
                ),
              );
            } catch (refreshError) {
              setError(readableError(refreshError));
            }
          }
        }
      })();
    },
    [applyEvent],
  );

  const hydrateActiveTurn = useCallback(
    async (legacy: ConversationSnapshot) => {
      const runtimeStatus = await chatApi.getRuntimeV2ConversationRuntimeStatus(
        legacy.conversation.id,
      );
      setRuntimeStatuses((current) => ({
        ...current,
        [legacy.conversation.id]: runtimeStatus,
      }));

      if (runtimeStatus.effectiveRuntime === "v2") {
        const laneList = await chatApi.listRuntimeV2Lanes(legacy.conversation.id);
        const mainLane =
          laneList.items.find((item: RuntimeV2Lane) => item.isMain) ??
          laneList.items.find(
            (item: RuntimeV2Lane) => item.id === laneList.activeLaneId,
          ) ??
          null;
        if (mainLane) {
          setMainLaneIds((current) => ({
            ...current,
            [legacy.conversation.id]: mainLane.id,
          }));
          setViewLaneIds((current) => ({
            ...current,
            [legacy.conversation.id]: mainLane.id,
          }));
        }
        setLaneTrees((current) => ({
          ...current,
          [legacy.conversation.id]: laneList.items,
        }));
        const runtime = await chatApi.getRuntimeV2Snapshot(
          legacy.conversation.id,
          mainLane?.id,
        );
        applyRuntimeSnapshot(legacy, runtime);
        if (runtime.runState) {
          followRuntimeConversation(
            legacy.conversation.id,
            runtime.lastEventSeq,
            mainLane?.id,
          );
        }
        return;
      }

      const latest = legacy.turns.at(-1);
      if (!latest || !["created", "running"].includes(latest.turn.status)) return;
      const compact = await chatApi.getTurn(latest.turn.id);
      setLiveTurns((current) => ({
        ...current,
        [latest.turn.id]: liveFromSnapshot(compact),
      }));
      followTurn(latest.turn.id, legacy.conversation.id, compact.lastSequence);
    },
    [applyRuntimeSnapshot, followRuntimeConversation, followTurn],
  );

  const loadConversation = useCallback(
    async (conversationId: string) => {
      setLoading(true);
      setError(null);
      try {
        const snapshot = await chatApi.getConversation(conversationId);
        setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
        await hydrateActiveTurn(snapshot);
      } catch (loadError) {
        setError(readableError(loadError));
      } finally {
        setLoading(false);
      }
    },
    [hydrateActiveTurn],
  );

  const openConversation = useCallback(
    async (conversationId: string) => {
      setActiveConversationId(conversationId);
      await loadConversation(conversationId);
    },
    [loadConversation],
  );

  const openSideConversation = useCallback(
    async (conversationId: string) => {
      setSideConversationId(conversationId);
      setSideDraft("");
      setSideLoading(true);
      setError(null);
      try {
        const snapshot = await chatApi.getConversation(conversationId);
        setSnapshots((current) => ({ ...current, [conversationId]: snapshot }));
        await hydrateActiveTurn(snapshot);
      } catch (loadError) {
        setError(readableError(loadError));
      } finally {
        setSideLoading(false);
      }
    },
    [hydrateActiveTurn],
  );

  const dismissSideConversation = useCallback(() => {
    if (sideLane) {
      streams.current
        .get(`v2:${sideLane.conversationId}:${sideLane.laneId}:side`)
        ?.abort();
      streams.current.delete(
        `v2:${sideLane.conversationId}:${sideLane.laneId}:side`,
      );
      setSideSnapshots((current) => {
        const next = { ...current };
        delete next[sideLane.laneId];
        return next;
      });
    }
    setSideLane(null);
    setSideConversationId(null);
  }, [sideLane]);

  const closeSideConversation = useCallback(async () => {
    if (sideLane?.mode === "temporary_conversation") {
      const confirmed = globalThis.confirm(
        "关闭后将删除这个临时对话，且无法恢复。确定继续吗？",
      );
      if (!confirmed) return false;

      setPendingAction("delete-temporary-conversation");
      setError(null);
      try {
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
        setRuntimeStatuses((current) => {
          const next = { ...current };
          delete next[sideLane.conversationId];
          return next;
        });
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
      } catch (deleteError) {
        setError(readableError(deleteError));
        return false;
      } finally {
        setPendingAction(null);
      }
    }
    dismissSideConversation();
    return true;
  }, [dismissSideConversation, sideLane, statusFilter, workspaceId]);

  const openLaneInSide = useCallback(
    async (conversationId: string, laneId: string) => {
      if (sideLane?.mode === "temporary_conversation") {
        const closed = await closeSideConversation();
        if (!closed) return;
      } else {
        dismissSideConversation();
      }

      setSideLoading(true);
      setError(null);
      try {
        const [legacy, runtime] = await Promise.all([
          chatApi.getConversation(conversationId),
          chatApi.getRuntimeV2Snapshot(conversationId, laneId),
        ]);
        setSideLane({
          conversationId,
          laneId,
          mode: "branch_lane",
        });
        setSideConversationId(conversationId);
        setSideDraft("");
        applyRuntimeSnapshot(legacy, runtime, "side");
        if (runtime.runState) {
          followRuntimeConversation(
            conversationId,
            runtime.lastEventSeq,
            laneId,
            "side",
          );
        }
      } catch (loadError) {
        setError(readableError(loadError));
      } finally {
        setSideLoading(false);
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
      setLoading(true);
      setError(null);
      try {
        let items = await chatApi.listConversations(status, undefined, workspaceId);
        if (status === "active" && items.length === 0) {
          const created = await chatApi.createConversation(workspaceId);
          items = [created];
        }
        setConversations(items);
        const target =
          items.find((item) => item.id === preferredId)?.id ?? items[0]?.id ?? null;
        if (target) await openConversation(target);
        else {
          setActiveConversationId(null);
          setLoading(false);
        }
      } catch (loadError) {
        setError(readableError(loadError));
        setLoading(false);
      }
    },
    [openConversation, workspaceId],
  );

  useEffect(() => {
    void chatApi.health().then(setHealth).catch(() => setHealth(null));
    void chatApi
      .capabilities()
      .then(setCapabilities)
      .catch(() => setCapabilities(null));
    void chatApi
      .listWorkspaces()
      .then(setWorkspaces)
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
    void loadConversationList(statusFilter, activeConversationId ?? undefined);
    // active id 不作为重载触发；切换工作区时列表整体换防。
  }, [statusFilter, workspaceId]);

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

  const sendRuntimeV2Message = async (
    conversationId: string,
    content: string,
    laneId?: string | null,
    target: SnapshotTarget = "main",
  ) => {
    const before = await chatApi.getRuntimeV2Snapshot(conversationId, laneId);
    const result = await chatApi.createRuntimeV2Message(
      conversationId,
      content,
      laneId ?? before.activeLaneId,
    );
    const legacy = await chatApi.getConversation(conversationId);
    const runtime = await chatApi.getRuntimeV2Snapshot(
      conversationId,
      result.laneId,
    );
    applyRuntimeSnapshot(legacy, runtime, target);
    followRuntimeConversation(
      conversationId,
      before.lastEventSeq,
      result.laneId,
      target,
    );
    return result;
  };

  const runCommand = async (
    action: string,
    operation: () => Promise<TurnCommandResponse>,
  ) => {
    setPendingAction(action);
    setError(null);
    try {
      const result = await operation();
      const compact = await chatApi.getTurn(result.turnId);
      setLiveTurns((current) => ({
        ...current,
        [result.turnId]: liveFromSnapshot(compact),
      }));
      const snapshot = await chatApi.getConversation(result.conversationId);
      setSnapshots((current) => ({ ...current, [result.conversationId]: snapshot }));
      followTurn(result.turnId, result.conversationId, compact.lastSequence);
    } catch (commandError) {
      setError(readableError(commandError));
    } finally {
      setPendingAction(null);
    }
  };

  const send = async () => {
    const content = draft.trim();
    if (!content || !activeConversationId || isGenerating) return;
    const retainedDraft = draft;
    setDraft("");
    setPendingAction("send");
    setError(null);
    try {
      if (runtimeStatuses[activeConversationId]?.effectiveRuntime === "v2") {
        await sendRuntimeV2Message(
          activeConversationId,
          content,
          viewLaneIds[activeConversationId] ?? mainLaneIds[activeConversationId],
        );
      } else {
        await runCommand("send", () =>
          chatApi.createTurn(activeConversationId, content, requestId()),
        );
      }
    } catch (sendError) {
      setDraft(retainedDraft);
      setError(readableError(sendError));
    } finally {
      setPendingAction(null);
    }
  };

  const sendSide = async () => {
    const content = sideDraft.trim();
    if (!content || !sideConversationId || sideIsGenerating) return;
    const retainedDraft = sideDraft;
    setSideDraft("");
    setPendingAction("send");
    setError(null);
    try {
      if (runtimeStatuses[sideConversationId]?.effectiveRuntime === "v2") {
        await sendRuntimeV2Message(
          sideConversationId,
          content,
          sideLane?.conversationId === sideConversationId
            ? sideLane.laneId
            : mainLaneIds[sideConversationId],
          sideLane?.conversationId === sideConversationId ? "side" : "main",
        );
      } else {
        await runCommand("send", () =>
          chatApi.createTurn(sideConversationId, content, requestId()),
        );
      }
    } catch (sendError) {
      setSideDraft(retainedDraft);
      setError(readableError(sendError));
    } finally {
      setPendingAction(null);
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

  const newConversation = async () => {
    setPendingAction("new");
    setError(null);
    try {
      const conversation = await chatApi.createConversation(workspaceId);
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

  const createTemporaryConversation = async (forkTurnId?: string) => {
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
      if (runtimeStatuses[activeConversationId]?.effectiveRuntime === "v2") {
        const laneList = await chatApi.listRuntimeV2Lanes(activeConversationId);
        const sourceLaneId =
          viewLaneIds[activeConversationId] ??
          mainLaneIds[activeConversationId] ??
          laneList.activeLaneId;
        if (!sourceLaneId) throw new Error("当前会话还没有可分叉的 Runtime v2 Lane。");
        const turnSnapshot = forkTurnId
          ? snapshots[activeConversationId]?.turns.find(
              (item) => item.turn.id === forkTurnId,
            )
          : undefined;
        const sourceLeafEntryId = turnSnapshot?.userMessage.id;
        if (forkTurnId && !sourceLeafEntryId) {
          throw new Error("无法定位临时对话的历史截止消息。");
        }
        const created = await chatApi.createRuntimeV2TemporaryConversation(
          activeConversationId,
          {
            sourceLaneId,
            ...(sourceLeafEntryId ? { sourceLeafEntryId } : {}),
          },
        );
        const temporaryConversationId = created.conversation.id;
        const [legacy, runtime, runtimeStatus] = await Promise.all([
          chatApi.getConversation(temporaryConversationId),
          chatApi.getRuntimeV2Snapshot(
            temporaryConversationId,
            created.lane.id,
          ),
          chatApi.getRuntimeV2ConversationRuntimeStatus(
            temporaryConversationId,
          ),
        ]);
        setRuntimeStatuses((current) => ({
          ...current,
          [temporaryConversationId]: runtimeStatus,
        }));
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
        return;
      }
      const { conversation } = await chatApi.createBranch(
        activeConversationId,
        forkTurnId,
      );
      setConversations((current) => [
        conversation,
        ...current.filter((item) => item.id !== conversation.id),
      ]);
      await openSideConversation(conversation.id);
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
      setPendingAction("switch-lane");
      setError(null);
      try {
        setViewLaneIds((current) => ({
          ...current,
          [conversationId]: laneId,
        }));
        const legacy = await chatApi.getConversation(conversationId);
        const runtime = await chatApi.getRuntimeV2Snapshot(conversationId, laneId);
        applyRuntimeSnapshot(legacy, runtime);
        followRuntimeConversation(conversationId, runtime.lastEventSeq, laneId);
      } catch (loadError) {
        setError(readableError(loadError));
      } finally {
        setPendingAction(null);
      }
    },
    [applyRuntimeSnapshot, followRuntimeConversation],
  );

  const forkLane = useCallback(
    async (conversationId: string, sourceLaneId: string, forkTurnId?: string) => {
      setPendingAction("fork-lane");
      setError(null);
      try {
        const turnSnapshot = forkTurnId
          ? snapshots[conversationId]?.turns.find(
              (item) => item.turn.id === forkTurnId,
            )
          : undefined;
        const baseEntryId = turnSnapshot?.userMessage.id;
        if (forkTurnId && !baseEntryId) {
          throw new Error("无法定位分叉点的用户消息。");
        }
        const created = await chatApi.createRuntimeV2Lane(conversationId, {
          sourceLaneId,
          ...(baseEntryId ? { baseEntryId } : {}),
        });
        const legacy = await chatApi.getConversation(conversationId);
        const runtime = await chatApi.getRuntimeV2Snapshot(
          conversationId,
          created.lane.id,
        );
        setViewLaneIds((current) => ({
          ...current,
          [conversationId]: created.lane.id,
        }));
        applyRuntimeSnapshot(legacy, runtime);
        followRuntimeConversation(
          conversationId,
          runtime.lastEventSeq,
          created.lane.id,
        );
        await refreshLaneTree(conversationId);
      } catch (forkError) {
        setError(readableError(forkError));
      } finally {
        setPendingAction(null);
      }
    },
    [
      applyRuntimeSnapshot,
      followRuntimeConversation,
      refreshLaneTree,
      snapshots,
    ],
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
              chatApi.getRuntimeV2Snapshot(conversationId, nextLaneId),
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

  const promoteConversation = async (conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("promote");
    setError(null);
    try {
      if (
        runtimeStatuses[targetId]?.effectiveRuntime === "v2" &&
        sideLane?.conversationId === targetId
      ) {
        if (sideLane.mode === "temporary_conversation") {
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
      const { conversation } = await chatApi.promoteConversation(targetId);
      setConversations((current) =>
        current.map((item) => (item.id === conversation.id ? conversation : item)),
      );
      setSnapshots((current) => {
        const snapshot = current[conversation.id];
        return snapshot
          ? { ...current, [conversation.id]: { ...snapshot, conversation } }
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
                  conversation,
                  ...current.filter((item) => item.id !== conversation.id),
                ]
              : current.filter((item) => item.id !== conversation.id),
          );
        }
      }
    } catch (promoteError) {
      setError(readableError(promoteError));
    } finally {
      setPendingAction(null);
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
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("provider");
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
    } catch (modelError) {
      setError(readableError(modelError));
    } finally {
      setPendingAction(null);
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

  const cancel = async (conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    const targetSnapshot =
      sideLane && sideLane.conversationId === targetId
        ? sideSnapshots[sideLane.laneId]
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
    setPendingAction("cancel");
    try {
      if (runtimeStatuses[targetId]?.effectiveRuntime === "v2") {
        await chatApi.cancelRuntimeV2Run(targetTurn.turn.id);
      } else {
        const compact = await chatApi.cancelTurn(targetTurn.turn.id);
        setLiveTurns((current) => ({
          ...current,
          [compact.turnId]: liveFromSnapshot(compact),
        }));
      }
    } catch (cancelError) {
      setError(readableError(cancelError));
    } finally {
      setPendingAction(null);
    }
  };

  const resolveApproval = async (
    turnId: string,
    approvalId: string,
    decision: "approve" | "deny",
  ) => {
    setPendingAction(`approval:${approvalId}`);
    setError(null);
    try {
      const conversationId = Object.entries(runtimeStatuses).find(
        ([id]) =>
          snapshots[id]?.turns.some((turn) => turn.turn.id === turnId) ||
          (sideLane?.conversationId === id &&
            sideSnapshots[sideLane.laneId]?.turns.some(
              (turn) => turn.turn.id === turnId,
            )),
      )?.[0];
      if (conversationId && runtimeStatuses[conversationId]?.effectiveRuntime === "v2") {
        await chatApi.resolveRuntimeV2Approval(approvalId, decision);
      } else {
        await chatApi.resolveApproval(approvalId, decision);
      }
      setLiveTurns((current) => {
        const live = current[turnId];
        if (!live || live.pendingApproval?.id !== approvalId) return current;
        return {
          ...current,
          [turnId]: { ...live, pendingApproval: undefined },
        };
      });
    } catch (approvalError) {
      setError(readableError(approvalError));
    } finally {
      setPendingAction(null);
    }
  };

  const runRuntimeV2Command = async (
    action: "retry" | "regenerate",
    conversationId: string,
    runId: string,
    laneId?: string | null,
  ) => {
    setPendingAction(action);
    setError(null);
    try {
      const before = await chatApi.getRuntimeV2Snapshot(conversationId, laneId);
      await chatApi.regenerateRuntimeV2Run(runId);
      const runtime = await chatApi.getRuntimeV2Snapshot(conversationId, laneId);
      const legacy = await chatApi.getConversation(runtime.conversationId);
      applyRuntimeSnapshot(legacy, runtime, laneId ? "side" : "main");
      followRuntimeConversation(
        runtime.conversationId,
        before.lastEventSeq,
        laneId,
        laneId ? "side" : "main",
      );
    } catch (commandError) {
      setError(readableError(commandError));
    } finally {
      setPendingAction(null);
    }
  };

  const retry = async (turnId: string, conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    const isSide =
      !!targetId && sideLane?.conversationId === targetId && conversationId === sideConversationId;
    if (runtimeStatuses[targetId ?? ""]?.effectiveRuntime === "v2") {
      await runRuntimeV2Command(
        "retry",
        targetId!,
        turnId,
        isSide ? sideLane.laneId : undefined,
      );
      return;
    }
    await runCommand("retry", () => chatApi.retryTurn(turnId, requestId()));
  };

  const regenerate = async (turnId: string, conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    const isSide =
      !!targetId && sideLane?.conversationId === targetId && conversationId === sideConversationId;
    if (runtimeStatuses[targetId ?? ""]?.effectiveRuntime === "v2") {
      await runRuntimeV2Command(
        "regenerate",
        targetId!,
        turnId,
        isSide ? sideLane.laneId : undefined,
      );
      return;
    }
    await runCommand("regenerate", () => chatApi.regenerateTurn(turnId, requestId()));
  };

  const selectVariant = async (
    turnId: string,
    variantId: string,
    conversationId?: string,
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("select");
    try {
      if (runtimeStatuses[targetId]?.effectiveRuntime === "v2") {
        await chatApi.selectRuntimeV2RunVariant(variantId);
        const laneId =
          sideLane?.conversationId === targetId && targetId === sideConversationId
            ? sideLane.laneId
            : (viewLaneIds[targetId] ?? mainLaneIds[targetId]);
        const runtime = await chatApi.getRuntimeV2Snapshot(targetId, laneId);
        const legacy = await chatApi.getConversation(targetId);
        applyRuntimeSnapshot(
          legacy,
          runtime,
          laneId && sideLane?.laneId === laneId ? "side" : "main",
        );
      } else {
        await chatApi.selectVariant(turnId, variantId);
        const snapshot = await chatApi.getConversation(targetId);
        setSnapshots((current) => ({ ...current, [targetId]: snapshot }));
        const compact = await chatApi.getTurn(turnId);
        setLiveTurns((current) => ({ ...current, [turnId]: liveFromSnapshot(compact) }));
      }
    } catch (selectionError) {
      setError(readableError(selectionError));
    } finally {
      setPendingAction(null);
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

  const createWorkspace = async (name: string) => {
    const workspace = await chatApi.createWorkspace(name);
    setWorkspaces((current) => [...current, workspace]);
    selectWorkspace(workspace.id);
    return workspace;
  };

  const refreshWorkspaces = async () => {
    try {
      const items = await chatApi.listWorkspaces();
      setWorkspaces(items);
    } catch {
      // 列表刷新失败保持现有状态，忽略。
    }
  };

  return {
    capabilities,
    activeConversationId,
    activeSnapshot,
    createWorkspace,
    refreshWorkspaces,
    selectWorkspace,
    workspaceId,
    workspaces,
    changeConversationStatus,
    closeSideConversation,
    conversations: visibleConversations,
    createTemporaryConversation,
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
    cancel,
    deleteConversation,
    draft,
    error,
    health,
    isGenerating,
    liveTurns,
    loading,
    newConversation,
    openConversation,
    pendingAction,
    providers,
    regenerate,
    removeFile,
    resolveApproval,
    renameConversation,
    retry,
    refreshCapabilities,
    refreshProviders,
    search,
    selectVariant,
    changeConversationModel,
    send,
    setDraft,
    setError,
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
