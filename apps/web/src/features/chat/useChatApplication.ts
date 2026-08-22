import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClientError, chatApi, streamTurnEvents } from "./api";
import type {
  CompactTurnSnapshot,
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
});

export function useChatApplication() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ConversationSnapshot>>({});
  const [liveTurns, setLiveTurns] = useState<Record<string, LiveTurn>>({});
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
      };

      if (event.type === "turn.started") next.status = "running";
      if (event.type === "message.started") next.content = "";
      if (event.type === "message.delta") next.content += event.data.delta ?? "";
      if (event.type === "message.completed") next.content = event.data.content ?? next.content;
      if (event.type === "turn.completed") next.status = "completed";
      if (event.type === "turn.failed") {
        next.status = "failed";
        next.content = event.data.partialContent ?? next.content;
        next.error = event.data.error;
      }
      if (event.type === "turn.cancelled") {
        next.status = "cancelled";
        next.content = event.data.partialContent ?? next.content;
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

  const openConversation = useCallback(
    async (conversationId: string) => {
      setActiveConversationId(conversationId);
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

  const renameConversation = async (title: string) => {
    if (!activeConversationId) return;
    setPendingAction("rename");
    try {
      const updated = await chatApi.patchConversation(activeConversationId, { title });
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

  const changeConversationStatus = async (status: ConversationStatus) => {
    if (!activeConversationId) return;
    setPendingAction("status");
    try {
      const updated = await chatApi.patchConversation(activeConversationId, { status });
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

  const deleteConversation = async () => {
    if (!activeConversationId) return;
    setPendingAction("delete");
    try {
      await chatApi.deleteConversation(activeConversationId);
      setSnapshots((current) => {
        const next = { ...current };
        delete next[activeConversationId];
        return next;
      });
      await loadConversationList(statusFilter);
    } catch (deleteError) {
      setError(readableError(deleteError));
    } finally {
      setPendingAction(null);
    }
  };

  const cancel = async () => {
    if (!latestTurn) return;
    setPendingAction("cancel");
    try {
      const compact = await chatApi.cancelTurn(latestTurn.turn.id);
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

  const retry = async (turnId: string) =>
    runCommand("retry", () => chatApi.retryTurn(turnId, requestId()));

  const regenerate = async (turnId: string) =>
    runCommand("regenerate", () => chatApi.regenerateTurn(turnId, requestId()));

  const selectVariant = async (turnId: string, variantId: string) => {
    if (!activeConversationId) return;
    setPendingAction("select");
    try {
      await chatApi.selectVariant(turnId, variantId);
      const snapshot = await chatApi.getConversation(activeConversationId);
      setSnapshots((current) => ({ ...current, [activeConversationId]: snapshot }));
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
    conversations: visibleConversations,
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
  };
}
