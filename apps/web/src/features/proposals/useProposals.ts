import { useCallback, useEffect, useRef, useState } from "react";
import { ApiClientError, chatApi } from "../chat/api";
import type {
  ArtifactProposal,
  ArtifactRecordSummary,
  KnowledgeProposal,
  MemoryProposal,
  TaskProposal,
} from "../chat/apiTypes";

export type TurnProposals = {
  artifacts: ArtifactProposal[];
  memories: MemoryProposal[];
  tasks: TaskProposal[];
  knowledge: KnowledgeProposal[];
};

type ConversationProposalState = TurnProposals;

const POLL_DELAYS_MS = [2000, 6000, 12000];

export function useProposals(
  activeConversationId: string | null,
  sideConversationId: string | null = null,
) {
  const [byConversation, setByConversation] = useState<
    Record<string, ConversationProposalState>
  >({});
  const [resolvedArtifacts, setResolvedArtifacts] = useState<
    Record<string, ArtifactRecordSummary>
  >({});
  const [busyProposalId, setBusyProposalId] = useState<string | null>(null);
  const [resolveErrors, setResolveErrors] = useState<Record<string, string>>({});
  const timers = useRef(new Map<string, number[]>());
  const watchedTurns = useRef(new Set<string>());

  const fetchProposals = useCallback(async (conversationId: string) => {
    try {
      const [artifacts, memories, tasks, knowledge] = await Promise.all([
        chatApi.listArtifactProposals(conversationId, true),
        chatApi.listMemoryProposals(conversationId, true),
        chatApi.listTaskProposals(conversationId, true),
        chatApi.listKnowledgeProposals(conversationId, true),
      ]);
      setByConversation((current) => ({
        ...current,
        [conversationId]: { artifacts, memories, tasks, knowledge },
      }));
    } catch {
      // 提案是增强信息，拉取失败时静默降级，不打断聊天主流程。
    }
  }, []);

  useEffect(() => {
    for (const pending of timers.current.values()) {
      pending.forEach((timer) => globalThis.clearTimeout(timer));
    }
    timers.current.clear();
    watchedTurns.current.clear();
    if (activeConversationId) void fetchProposals(activeConversationId);
  }, [activeConversationId, fetchProposals]);

  useEffect(() => {
    if (sideConversationId) void fetchProposals(sideConversationId);
  }, [sideConversationId, fetchProposals]);

  const refreshVisibleProposals = useCallback(async () => {
    if (activeConversationId) await fetchProposals(activeConversationId);
    if (sideConversationId) await fetchProposals(sideConversationId);
  }, [activeConversationId, fetchProposals, sideConversationId]);

  const watchTurn = useCallback((conversationId: string, turnId: string) => {
    const key = `${conversationId}:${turnId}`;
    if (watchedTurns.current.has(key)) return;
    watchedTurns.current.add(key);

    const scheduled: number[] = [];
    POLL_DELAYS_MS.forEach((delay) => {
      const timer = globalThis.setTimeout(() => {
        void (async () => {
          try {
            const [artifacts, memories, tasks, knowledge] = await Promise.all([
              chatApi.listArtifactProposals(conversationId, true),
              chatApi.listMemoryProposals(conversationId, true),
              chatApi.listTaskProposals(conversationId, true),
              chatApi.listKnowledgeProposals(conversationId, true),
            ]);
            setByConversation((current) => ({
              ...current,
              [conversationId]: { artifacts, memories, tasks, knowledge },
            }));
            const found =
              artifacts.some((item) => item.turnId === turnId) ||
              memories.some((item) => item.turnId === turnId) ||
              tasks.some((item) => item.turnId === turnId) ||
              knowledge.some((item) => item.turnId === turnId);
            if (found) {
              scheduled.forEach((pending) => globalThis.clearTimeout(pending));
            }
          } catch {
            // 轮询失败交给下一轮；三次后自然停止。
          }
        })();
      }, delay);
      scheduled.push(timer);
    });
    timers.current.set(key, scheduled);
  }, []);

  const clearError = useCallback((proposalId: string) => {
    setResolveErrors((current) => {
      if (!(proposalId in current)) return current;
      const next = { ...current };
      delete next[proposalId];
      return next;
    });
  }, []);

  const resolveArtifactProposal = useCallback(
    async (proposalId: string, decision: "accept" | "reject") => {
      setBusyProposalId(proposalId);
      clearError(proposalId);
      try {
        const result = await chatApi.resolveArtifactProposal(proposalId, decision);
        if (result.artifact) {
          setResolvedArtifacts((current) => ({
            ...current,
            [proposalId]: result.artifact as ArtifactRecordSummary,
          }));
        }
        await refreshVisibleProposals();
      } catch (error) {
        const message =
          error instanceof ApiClientError && error.status === 409
            ? "文档已有更新，请在聊天中重新提出修改。"
            : "提案处理失败，请重试。";
        setResolveErrors((current) => ({ ...current, [proposalId]: message }));
      } finally {
        setBusyProposalId(null);
      }
    },
    [clearError, refreshVisibleProposals],
  );

  const resolveMemoryProposal = useCallback(
    async (proposalId: string, decision: "accept" | "reject") => {
      setBusyProposalId(proposalId);
      clearError(proposalId);
      try {
        await chatApi.resolveMemoryProposal(proposalId, decision);
        await refreshVisibleProposals();
      } catch (error) {
        const message =
          error instanceof ApiClientError && error.status >= 400 && error.status < 500
            ? error.message
            : "记忆提案处理失败，请重试。";
        setResolveErrors((current) => ({ ...current, [proposalId]: message }));
      } finally {
        setBusyProposalId(null);
      }
    },
    [clearError, refreshVisibleProposals],
  );

  const resolveTaskProposal = useCallback(
    async (proposalId: string, decision: "accept" | "reject") => {
      setBusyProposalId(proposalId);
      clearError(proposalId);
      try {
        await chatApi.resolveTaskProposal(proposalId, decision);
        await refreshVisibleProposals();
      } catch (error) {
        const message =
          error instanceof ApiClientError && error.status === 409
            ? "该提案已处理。"
            : "提案处理失败，请重试。";
        setResolveErrors((current) => ({ ...current, [proposalId]: message }));
      } finally {
        setBusyProposalId(null);
      }
    },
    [clearError, refreshVisibleProposals],
  );

  const resolveKnowledgeProposal = useCallback(
    async (
      proposalId: string,
      decision: "accept" | "reject",
      workspaceId?: string | null,
    ) => {
      setBusyProposalId(proposalId);
      clearError(proposalId);
      try {
        await chatApi.resolveKnowledgeProposal(proposalId, decision, workspaceId);
        await refreshVisibleProposals();
      } catch (error) {
        const message =
          error instanceof ApiClientError && error.status >= 400 && error.status < 500
            ? error.message
            : "知识提案处理失败，请重试。";
        setResolveErrors((current) => ({ ...current, [proposalId]: message }));
      } finally {
        setBusyProposalId(null);
      }
    },
    [clearError, refreshVisibleProposals],
  );

  const artifactProposalsFor = useCallback(
    (conversationId: string | null): ArtifactProposal[] => {
      if (!conversationId) return [];
      return byConversation[conversationId]?.artifacts ?? [];
    },
    [byConversation],
  );

  const forTurn = useCallback(
    (conversationId: string | null, turnId: string): TurnProposals => {
      const state = conversationId ? byConversation[conversationId] : undefined;
      if (!state) {
        return { artifacts: [], memories: [], tasks: [], knowledge: [] };
      }
      return {
        artifacts: state.artifacts.filter((item) => item.turnId === turnId),
        memories: state.memories.filter((item) => item.turnId === turnId),
        tasks: state.tasks.filter((item) => item.turnId === turnId),
        knowledge: state.knowledge.filter((item) => item.turnId === turnId),
      };
    },
    [byConversation],
  );

  return {
    artifactProposalsFor,
    busyProposalId,
    forTurn,
    resolveArtifactProposal,
    resolveKnowledgeProposal,
    resolveMemoryProposal,
    resolveTaskProposal,
    resolvedArtifacts,
    resolveErrors,
    watchTurn,
  };
}
