import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClientError, chatApi, streamTurnEvents } from "./api";
import type {
  CompactTurnSnapshot,
  ActivityStatus,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  LiveTurn,
  RuntimeEvent,
  TurnCommandResponse,
} from "./apiTypes";

const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

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

export function useChatApplication() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ConversationSnapshot>>({});
  const [liveTurns, setLiveTurns] = useState<Record<string, LiveTurn>>({});
  const [sideConversationId, setSideConversationId] = useState<string | null>(null);
  const [sideDraft, setSideDraft] = useState("");
  const [sideLoading, setSideLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ConversationStatus>("active");
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const streams = useRef(new Map<string, AbortController>());

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
    async (snapshot: ConversationSnapshot) => {
      const latest = snapshot.turns.at(-1);
      if (!latest || !["created", "running"].includes(latest.turn.status)) return;
      const compact = await chatApi.getTurn(latest.turn.id);
      setLiveTurns((current) => ({
        ...current,
        [latest.turn.id]: liveFromSnapshot(compact),
      }));
      followTurn(latest.turn.id, snapshot.conversation.id, compact.lastSequence);
    },
    [followTurn],
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
      setSideConversationId(null);
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

  const closeSideConversation = useCallback(() => {
    setSideConversationId(null);
  }, []);

  const openBranchInSide = useCallback(
    async (branchId: string, parentId?: string) => {
      if (parentId && parentId !== activeConversationId) {
        setActiveConversationId(parentId);
        await loadConversation(parentId);
      }
      await openSideConversation(branchId);
    },
    [activeConversationId, loadConversation, openSideConversation],
  );

  const loadConversationList = useCallback(
    async (status: ConversationStatus, preferredId?: string) => {
      setLoading(true);
      setError(null);
      try {
        let items = await chatApi.listConversations(status);
        if (status === "active" && items.length === 0) {
          const created = await chatApi.createConversation();
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
    [openConversation],
  );

  useEffect(() => {
    void chatApi.health().then(setHealth).catch(() => setHealth(null));
    void loadConversationList(statusFilter, activeConversationId ?? undefined);
  }, [statusFilter]); // The active id is intentionally not a reload trigger.

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
  const isGenerating = activeTurnStatus === "created" || activeTurnStatus === "running";

  const sideSnapshot = sideConversationId
    ? snapshots[sideConversationId] ?? null
    : null;
  const sideLatestTurn = sideSnapshot?.turns.at(-1);
  const sideLatestLiveTurn = sideLatestTurn
    ? liveTurns[sideLatestTurn.turn.id]
    : undefined;
  const sideTurnStatus = sideLatestLiveTurn?.status ?? sideLatestTurn?.turn.status;
  const sideIsGenerating =
    sideTurnStatus === "created" || sideTurnStatus === "running";

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
    await runCommand("send", async () => {
      try {
        return await chatApi.createTurn(activeConversationId, content, requestId());
      } catch (sendError) {
        setDraft(retainedDraft);
        throw sendError;
      }
    });
  };

  const sendSide = async () => {
    const content = sideDraft.trim();
    if (!content || !sideConversationId || sideIsGenerating) return;
    const retainedDraft = sideDraft;
    setSideDraft("");
    await runCommand("send", async () => {
      try {
        return await chatApi.createTurn(sideConversationId, content, requestId());
      } catch (sendError) {
        setSideDraft(retainedDraft);
        throw sendError;
      }
    });
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
      const conversation = await chatApi.createConversation();
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

  const createBranch = async (forkTurnId?: string) => {
    if (!activeConversationId) return;
    setPendingAction("branch");
    setError(null);
    try {
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

  const promoteConversation = async (conversationId?: string) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("promote");
    setError(null);
    try {
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
        await openConversation(conversation.id);
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
    const targetTurn = targetId ? snapshots[targetId]?.turns.at(-1) : latestTurn;
    if (
      !targetTurn ||
      !targetId ||
      targetTurn.turn.conversationId !== targetId
    ) {
      return;
    }
    setPendingAction("cancel");
    try {
      const compact = await chatApi.cancelTurn(targetTurn.turn.id);
      setLiveTurns((current) => ({
        ...current,
        [compact.turnId]: liveFromSnapshot(compact),
      }));
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
      await chatApi.resolveApproval(approvalId, decision);
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

  const retry = async (turnId: string) =>
    runCommand("retry", () => chatApi.retryTurn(turnId, requestId()));

  const regenerate = async (turnId: string) =>
    runCommand("regenerate", () => chatApi.regenerateTurn(turnId, requestId()));

  const selectVariant = async (
    turnId: string,
    variantId: string,
    conversationId?: string,
  ) => {
    const targetId = conversationId ?? activeConversationId;
    if (!targetId) return;
    setPendingAction("select");
    try {
      await chatApi.selectVariant(turnId, variantId);
      const snapshot = await chatApi.getConversation(targetId);
      setSnapshots((current) => ({ ...current, [targetId]: snapshot }));
      const compact = await chatApi.getTurn(turnId);
      setLiveTurns((current) => ({ ...current, [turnId]: liveFromSnapshot(compact) }));
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

  return {
    activeConversationId,
    activeSnapshot,
    changeConversationStatus,
    closeSideConversation,
    conversations: visibleConversations,
    createBranch,
    openBranchInSide,
    openSideConversation,
    promoteConversation,
    sendSide,
    setSideDraft,
    sideConversationId,
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
    regenerate,
    removeFile,
    resolveApproval,
    renameConversation,
    retry,
    search,
    selectVariant,
    send,
    setDraft,
    setError,
    setSearch,
    setStatusFilter,
    statusFilter,
    uploadFile,
  };
}
