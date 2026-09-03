import { useCallback, useEffect, useReducer, useRef } from "react";
import { chatApi, streamRuntimeV2Events } from "./api";
import type {
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";

export type RuntimeConnectionPhase =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting";

export type RuntimeTarget = {
  conversationId: string;
  laneId: string | null;
};

type RuntimeConnection = {
  phase: RuntimeConnectionPhase;
  error: string | null;
};

export type RuntimeCommandState = {
  pendingAction: string | null;
  error: string | null;
};

type RuntimeControllerState = {
  snapshots: Record<string, RuntimeV2Snapshot>;
  events: Record<string, RuntimeV2ProductEvent[]>;
  connections: Record<string, RuntimeConnection>;
  commands: Record<string, RuntimeCommandState>;
  lastEventSequences: Record<string, number>;
  latestEvent: RuntimeV2ProductEvent | null;
};

type RuntimeControllerAction =
  | {
      type: "snapshot_received";
      target: RuntimeTarget;
      snapshot: RuntimeV2Snapshot;
    }
  | { type: "event_received"; event: RuntimeV2ProductEvent }
  | {
      type: "connection_changed";
      conversationId: string;
      connection: RuntimeConnection;
    }
  | {
      type: "command_changed";
      target: RuntimeTarget;
      command: RuntimeCommandState;
    }
  | { type: "target_removed"; target: RuntimeTarget };

const initialState: RuntimeControllerState = {
  snapshots: {},
  events: {},
  connections: {},
  commands: {},
  lastEventSequences: {},
  latestEvent: null,
};

export const runtimeTargetKey = ({ conversationId, laneId }: RuntimeTarget) =>
  `${conversationId}\u0000${laneId ?? "main"}`;

const terminalRunStatuses = new Set(["completed", "failed", "cancelled"]);

const applyRuntimeEvent = (
  snapshot: RuntimeV2Snapshot,
  event: RuntimeV2ProductEvent,
): RuntimeV2Snapshot => {
  const next: RuntimeV2Snapshot = {
    ...snapshot,
    lastEventSeq: Math.max(snapshot.lastEventSeq, event.eventSeq),
  };

  if (event.type === "message.updated" && next.runState?.runId === event.runId) {
    next.runState = {
      ...next.runState,
      partialContent: next.runState.partialContent + (event.data.delta ?? ""),
    };
  }

  if (
    (event.type === "run.started" || event.type === "run.status_changed") &&
    next.runState?.runId === event.runId
  ) {
    next.runState = {
      ...next.runState,
      status: String(event.data.status ?? "running"),
    };
  }

  if (
    (event.type === "run.finished" ||
      event.type === "run.failed" ||
      event.type === "run.cancelled") &&
    next.runState?.runId === event.runId
  ) {
    next.runState = {
      ...next.runState,
      status:
        event.type === "run.finished"
          ? "completed"
          : event.type === "run.failed"
            ? "failed"
            : "cancelled",
      errorCode:
        event.type === "run.failed"
          ? String(event.data.errorCode ?? "runtime_failed")
          : next.runState.errorCode,
      safeMessage:
        event.type === "run.failed"
          ? String(event.data.safeMessage ?? "Runtime v2 执行失败。")
          : next.runState.safeMessage,
    };
  }

  if (
    event.type === "approval.requested" &&
    event.data.approvalId &&
    (!event.laneId || snapshot.activeLaneId === event.laneId)
  ) {
    next.pendingApprovals = [
      ...next.pendingApprovals.filter(
        (approval) => approval.id !== event.data.approvalId,
      ),
      {
        id: String(event.data.approvalId),
        runId: event.runId ?? "",
        modelTurnId: String(event.data.modelTurnId ?? ""),
        toolExecutionId: String(event.data.toolExecutionId ?? ""),
        toolName: String(event.data.toolName ?? "工具"),
        summary: String(event.data.summary ?? "等待工具审批"),
        reason: String(event.data.reason ?? ""),
      },
    ];
  }

  if (
    event.type === "approval.resolved" &&
    event.data.approvalId &&
    (!event.laneId || snapshot.activeLaneId === event.laneId)
  ) {
    next.pendingApprovals = next.pendingApprovals.filter(
      (approval) => approval.id !== event.data.approvalId,
    );
  }

  if (
    event.type.startsWith("tool_execution.") &&
    event.data.toolExecutionId &&
    event.data.status &&
    (!event.laneId || snapshot.activeLaneId === event.laneId)
  ) {
    const toolId = String(event.data.toolExecutionId);
    const exists = next.toolStates.some((tool) => tool.id === toolId);
    next.toolStates = exists
      ? next.toolStates.map((tool) =>
          tool.id === toolId
            ? {
                ...tool,
                modelTurnId:
                  event.data.modelTurnId === undefined
                    ? tool.modelTurnId
                    : String(event.data.modelTurnId),
                callId:
                  event.data.callId === undefined
                    ? tool.callId
                    : String(event.data.callId ?? ""),
                toolName:
                  event.data.toolName === undefined
                    ? tool.toolName
                    : String(event.data.toolName),
                status: String(event.data.status),
                errorCode:
                  event.data.errorCode === undefined
                    ? tool.errorCode
                    : event.data.errorCode,
                safeMessage:
                  event.data.safeMessage === undefined
                    ? tool.safeMessage
                    : event.data.safeMessage,
                retryable:
                  event.data.retryable === undefined
                    ? tool.retryable
                    : event.data.retryable,
                correlationId:
                  event.data.correlationId === undefined
                    ? tool.correlationId
                    : event.data.correlationId,
                errorDetails:
                  event.data.errorDetails === undefined
                    ? tool.errorDetails
                    : event.data.errorDetails,
                resultEntryId:
                  event.data.resultEntryId === undefined
                    ? tool.resultEntryId
                    : event.data.resultEntryId,
              }
            : tool,
        )
      : [
          ...next.toolStates,
          {
            id: toolId,
            runId: event.runId ?? "",
            modelTurnId: String(event.data.modelTurnId ?? ""),
            callId: String(event.data.callId ?? ""),
            toolName: String(event.data.toolName ?? "工具"),
            status: String(event.data.status),
            errorCode: event.data.errorCode ?? null,
            safeMessage: event.data.safeMessage ?? null,
            retryable: event.data.retryable ?? null,
            correlationId: event.data.correlationId ?? null,
            errorDetails: event.data.errorDetails ?? null,
            resultEntryId: event.data.resultEntryId ?? null,
          },
        ];
  }

  if (
    event.runId &&
    event.laneId &&
    (event.type === "run.started" || event.type === "run.status_changed") &&
    !terminalRunStatuses.has(String(event.data.status ?? "running"))
  ) {
    next.runningLaneId = event.laneId;
    next.runningRunId = event.runId;
  }

  if (
    event.runId === next.runningRunId &&
    (event.type === "run.finished" ||
      event.type === "run.failed" ||
      event.type === "run.cancelled")
  ) {
    next.runningLaneId = null;
    next.runningRunId = null;
  }

  return next;
};

export const runtimeControllerReducer = (
  state: RuntimeControllerState,
  action: RuntimeControllerAction,
): RuntimeControllerState => {
  if (action.type === "snapshot_received") {
    const conversationId = action.target.conversationId;
    const targetKey = runtimeTargetKey(action.target);
    const currentSnapshot = state.snapshots[targetKey];
    if (
      currentSnapshot &&
      currentSnapshot.lastEventSeq > action.snapshot.lastEventSeq
    ) {
      return state;
    }
    return {
      ...state,
      snapshots: {
        ...state.snapshots,
        [targetKey]: action.snapshot,
      },
      lastEventSequences: {
        ...state.lastEventSequences,
        [conversationId]: Math.max(
          state.lastEventSequences[conversationId] ?? 0,
          action.snapshot.lastEventSeq,
        ),
      },
    };
  }

  if (action.type === "event_received") {
    const event = action.event;
    if (event.eventSeq <= (state.lastEventSequences[event.conversationId] ?? 0)) {
      return state;
    }
    const snapshots = Object.fromEntries(
      Object.entries(state.snapshots).map(([key, snapshot]) => {
        const sameConversation = snapshot.conversationId === event.conversationId;
        return [
          key,
          sameConversation ? applyRuntimeEvent(snapshot, event) : snapshot,
        ];
      }),
    );
    return {
      ...state,
      snapshots,
      events: {
        ...state.events,
        [event.conversationId]: [
          ...(state.events[event.conversationId] ?? []),
          event,
        ].slice(-200),
      },
      lastEventSequences: {
        ...state.lastEventSequences,
        [event.conversationId]: event.eventSeq,
      },
      latestEvent: event,
    };
  }

  if (action.type === "connection_changed") {
    return {
      ...state,
      connections: {
        ...state.connections,
        [action.conversationId]: action.connection,
      },
    };
  }

  if (action.type === "command_changed") {
    return {
      ...state,
      commands: {
        ...state.commands,
        [runtimeTargetKey(action.target)]: action.command,
      },
    };
  }

  const snapshots = { ...state.snapshots };
  const commands = { ...state.commands };
  const targetKey = runtimeTargetKey(action.target);
  delete snapshots[targetKey];
  delete commands[targetKey];
  return { ...state, snapshots, commands };
};

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Runtime v2 事件流连接失败。";

export function useConversationRuntimeController() {
  const [state, dispatch] = useReducer(runtimeControllerReducer, initialState);
  const stateRef = useRef(state);
  const streamsRef = useRef(new Map<string, AbortController>());
  const targetsRef = useRef(new Map<string, RuntimeTarget>());
  const refreshTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const loadSnapshot = useCallback(async (target: RuntimeTarget) => {
    const snapshot = await chatApi.getRuntimeV2Snapshot(
      target.conversationId,
      target.laneId,
    );
    targetsRef.current.set(runtimeTargetKey(target), target);
    dispatch({ type: "snapshot_received", target, snapshot });
    return snapshot;
  }, []);

  const refreshConversationTargets = useCallback(
    async (conversationId: string) => {
      const targets = [...targetsRef.current.values()].filter(
        (target) => target.conversationId === conversationId,
      );
      if (!targets.length) {
        return loadSnapshot({ conversationId, laneId: null });
      }
      const snapshots = await Promise.all(
        targets.map((target) => loadSnapshot(target)),
      );
      return snapshots.find((snapshot) => snapshot.runningRunId) ?? snapshots[0];
    },
    [loadSnapshot],
  );

  // Coalesced live refresh: while a run is active the server pushes product
  // events (run/tool/message) but not full snapshots, so the ordered tool
  // surface (entries + toolStates) would otherwise stay stale/empty until the
  // stream closes. Refresh the authoritative snapshot shortly after each
  // live-progress event so the tool trace renders as tools start/complete.
  const scheduleRefresh = useCallback(
    (conversationId: string) => {
      const existing = refreshTimersRef.current.get(conversationId);
      if (existing) clearTimeout(existing);
      const timer = globalThis.setTimeout(() => {
        refreshTimersRef.current.delete(conversationId);
        void refreshConversationTargets(conversationId);
      }, 220);
      refreshTimersRef.current.set(conversationId, timer);
    },
    [refreshConversationTargets],
  );

  const followConversation = useCallback(
    (conversationId: string, initialSequence = 0) => {
      if (streamsRef.current.has(conversationId)) return;
      const controller = new AbortController();
      streamsRef.current.set(conversationId, controller);

      void (async () => {
        let cursor = Math.max(
          initialSequence,
          stateRef.current.lastEventSequences[conversationId] ?? 0,
        );
        let reconnecting = false;
        try {
          while (!controller.signal.aborted) {
            dispatch({
              type: "connection_changed",
              conversationId,
              connection: {
                phase: reconnecting ? "reconnecting" : "connecting",
                error: null,
              },
            });
            try {
              await streamRuntimeV2Events({
                conversationId,
                afterSequence: cursor,
                signal: controller.signal,
                onSnapshot: (snapshot) => {
                  if (controller.signal.aborted) return;
                  cursor = Math.max(cursor, snapshot.lastEventSeq);
                  const target = {
                    conversationId,
                    laneId: snapshot.activeLaneId,
                  };
                  targetsRef.current.set(runtimeTargetKey(target), target);
                  dispatch({ type: "snapshot_received", target, snapshot });
                  dispatch({
                    type: "connection_changed",
                    conversationId,
                    connection: { phase: "connected", error: null },
                  });
                },
                onProductEvent: (event) => {
                  if (controller.signal.aborted || event.eventSeq <= cursor) return;
                  cursor = event.eventSeq;
                  dispatch({ type: "event_received", event });
                  const live = event.type.startsWith("tool_execution.") ||
                    event.type === "run.started" ||
                    event.type === "run.status_changed" ||
                    event.type === "run.finished" ||
                    event.type === "run.failed" ||
                    event.type === "run.cancelled" ||
                    event.type === "message.updated";
                  if (live) scheduleRefresh(conversationId);
                },
              });

              const snapshot = await refreshConversationTargets(conversationId);
              cursor = Math.max(cursor, snapshot.lastEventSeq);
              if (!snapshot.runningRunId) break;
              reconnecting = true;
            } catch (error) {
              if (controller.signal.aborted) return;
              reconnecting = true;
              dispatch({
                type: "connection_changed",
                conversationId,
                connection: {
                  phase: "reconnecting",
                  error: errorMessage(error),
                },
              });
            }
            await new Promise((resolve) => globalThis.setTimeout(resolve, 800));
          }
        } finally {
          if (streamsRef.current.get(conversationId) === controller) {
            streamsRef.current.delete(conversationId);
            dispatch({
              type: "connection_changed",
              conversationId,
              connection: { phase: "idle", error: null },
            });
          }
        }
      })();
    },
    [refreshConversationTargets, scheduleRefresh],
  );

  const stopConversation = useCallback((conversationId: string) => {
    const controller = streamsRef.current.get(conversationId);
    if (!controller) return;
    controller.abort();
    if (streamsRef.current.get(conversationId) === controller) {
      streamsRef.current.delete(conversationId);
      dispatch({
        type: "connection_changed",
        conversationId,
        connection: { phase: "idle", error: null },
      });
    }
  }, []);

  const removeTarget = useCallback((target: RuntimeTarget) => {
    targetsRef.current.delete(runtimeTargetKey(target));
    dispatch({ type: "target_removed", target });
  }, []);

  const setCommand = useCallback(
    (target: RuntimeTarget, pendingAction: string | null, error: string | null) => {
      dispatch({
        type: "command_changed",
        target,
        command: { pendingAction, error },
      });
    },
    [],
  );

  useEffect(
    () => () => {
      for (const controller of streamsRef.current.values()) controller.abort();
      streamsRef.current.clear();
      targetsRef.current.clear();
    },
    [],
  );

  return {
    ...state,
    followConversation,
    loadSnapshot,
    removeTarget,
    setCommand,
    stopConversation,
  };
}