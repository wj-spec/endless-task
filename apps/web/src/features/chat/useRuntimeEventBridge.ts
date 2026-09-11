import { useCallback, useEffect, useRef } from "react";
import { chatApi } from "./api";
import {
  runtimeTargetKey,
  useConversationRuntimeController,
} from "./runtimeController";
import {
  runtimeSnapshotToConversationSnapshot,
  runtimeStatusToTurnStatus,
} from "./conversationSnapshotMapper";
import {
  activeRuntimeStatuses,
  liveFromRuntimeSnapshot,
} from "./chatApplicationSupport";
import type {
  ActivityStatus,
  Conversation,
  ConversationSnapshot,
  LiveTurn,
  RuntimeV2Lane,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";
import type { SideLaneTarget, SnapshotTarget } from "./chatApplicationSupport";

/**
 * 运行时事件桥：把 v2 运行时推来的快照与产品事件投影成界面状态。
 *
 * 从 `useChatApplication.ts`（2 048 行）抽出——这是那个 hook 里最"横"的一块：它自己
 * 管三份 ref（target 绑定表 / 已应用快照 / 已应用事件 id）、两个 effect、四个回调
 *（`unbindRuntimeTarget` / `applyRuntimeSnapshot` / `applyRuntimeProductEvent` /
 * `followRuntimeConversation` / `hydrateActiveTurn`），却与"会话列表/工作区/侧栏"这些
 * 领域几乎无关。
 *
 * **拆法要点**：它只依赖 8 个外部量，全部作为参数传入——
 * `runtimeController`（由 `useConversationRuntimeController` 创建后传入，不在这里再建一份，
 * 否则会出现两套 SSE 连接）、`snapshots`/`sideSnapshots`（投影时作为 legacy 基线）、
 * `viewLaneIds`（刷新恢复车道）与 5 个 setter。
 *
 * 行为零改动：成员逐行原样搬运，只把闭包里的量改成参数解构。
 */
export type RuntimeEventBridgeParams = {
  runtimeController: ReturnType<typeof useConversationRuntimeController>;
  snapshots: Record<string, ConversationSnapshot>;
  sideSnapshots: Record<string, ConversationSnapshot>;
  viewLaneIds: Record<string, string>;
  primaryConversationId: string | null;
  setSnapshots: React.Dispatch<React.SetStateAction<Record<string, ConversationSnapshot>>>;
  setSideSnapshots: React.Dispatch<
    React.SetStateAction<Record<string, ConversationSnapshot>>
  >;
  setLiveTurns: React.Dispatch<React.SetStateAction<Record<string, LiveTurn>>>;
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  setMainLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setViewLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setLaneTrees: React.Dispatch<
    React.SetStateAction<Record<string, RuntimeV2Lane[]>>
  >;
};

export function useRuntimeEventBridge({
  runtimeController,
  snapshots,
  sideSnapshots,
  viewLaneIds,
  primaryConversationId,
  setSnapshots,
  setSideSnapshots,
  setLiveTurns,
  setConversations,
  setMainLaneIds,
  setViewLaneIds,
  setLaneTrees,
}: RuntimeEventBridgeParams) {
  const runtimeSnapshotBindings = useRef(
    new Map<string, Set<SnapshotTarget>>(),
  );
  const appliedRuntimeSnapshots = useRef(new Map<string, RuntimeV2Snapshot>());
  const appliedRuntimeEventId = useRef<string | null>(null);

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

  return {
    applyRuntimeSnapshot,
    applyRuntimeProductEvent,
    followRuntimeConversation,
    hydrateActiveTurn,
    unbindRuntimeTarget,
  };
}
