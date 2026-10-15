import { chatApi } from "./api";
import { readableError } from "./apiErrorText";
import type {
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
} from "./apiTypes";
import type {
  SideLaneTarget,
  SnapshotTarget,
} from "./chatApplicationSupport";
import type { useConversationRuntimeController } from "./runtimeController";

/**
 * 会话生命周期：新建（含临时对话）/ 提升 / 重命名 / 换模型 / 改状态 / 删除。
 *
 * 从 `useChatApplication.ts`（1 365 行）抽出，共 282 行。这一簇的边界比"消息与提案"
 * 那一簇清楚：它主要写 `conversations` 列表、调 `openConversation`，并顺带处理
 * "被删除/被提升的会话是否正开在侧栏"。
 *
 * **行区间是人工确认后写死的**：上一轮用括号深度自动推断边界出过一次错（截断成员、
 * 多吞 `useState`），这轮 7 个区间逐个打印首末行核对过。
 *
 * 行为零改动：7 个成员逐行原样搬运，只把闭包里的量改成参数解构。
 */
export type ConversationLifecycleParams = {
  runtimeController: ReturnType<typeof useConversationRuntimeController>;
  activeConversationId: string | null;
  workspaceId: string | null;
  currentWorkspace: { id: string; name: string; rootPath?: string | null } | null;
  statusFilter: ConversationStatus;
  snapshots: Record<string, ConversationSnapshot>;
  sideLane: SideLaneTarget | null;
  sideConversationId: string | null;
  setSideConversationId: React.Dispatch<React.SetStateAction<string | null>>;
  setSideLane: React.Dispatch<React.SetStateAction<SideLaneTarget | null>>;
  setSideDraft: React.Dispatch<React.SetStateAction<string>>;
  setWorkspaceId: React.Dispatch<React.SetStateAction<string | null>>;
  workspaces: import("./apiTypes").Workspace[];
  /** 删除后重建会话列表（父层提供）。 */
  loadConversationList: (
    status: ConversationStatus,
    preferredId?: string,
  ) => Promise<void>;
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  setActiveConversationId: React.Dispatch<React.SetStateAction<string | null>>;
  setSnapshots: React.Dispatch<
    React.SetStateAction<Record<string, ConversationSnapshot>>
  >;
  setPendingAction: React.Dispatch<React.SetStateAction<string | null>>;
  setStatusFilter: React.Dispatch<React.SetStateAction<ConversationStatus>>;
  setError: React.Dispatch<React.SetStateAction<string | null>>;
  setMainLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setViewLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setLaneTrees: React.Dispatch<React.SetStateAction<Record<string, import("./apiTypes").RuntimeV2Lane[]>>>;
  primaryCommandTarget: { conversationId: string; laneId: string | null } | null;
  sideCommandTarget: { conversationId: string; laneId: string | null } | null;
  /** 刷新某会话的车道树（车道簇提供）。 */
  refreshLaneTree: (conversationId: string) => Promise<void>;
  setCommandFeedback: (
    target: { conversationId: string; laneId: string | null } | null,
    nextPendingAction: string | null,
    nextError: string | null,
  ) => void;
  bumpConversationsRevision: () => void;
  openConversation: (
    conversationId: string,
    options?: { focusTurnId?: string | null },
  ) => Promise<void>;
  loadConversation: (conversationId: string) => Promise<void>;
  applyRuntimeSnapshot: (
    legacy: ConversationSnapshot,
    runtime: import("./apiTypes").RuntimeV2Snapshot,
    target?: "main" | "side",
  ) => void;
  dismissSideConversation: (preserveTemporary?: boolean) => void;
  closeSideConversation: () => Promise<boolean>;
};

export function useConversationLifecycle({
  runtimeController,
  activeConversationId,
  workspaceId,
  currentWorkspace,
  statusFilter,
  snapshots,
  sideLane,
  sideConversationId,
  setSideConversationId,
  setSideLane,
  setSideDraft,
  setWorkspaceId,
  workspaces,
  loadConversationList,
  setConversations,
  setActiveConversationId,
  setSnapshots,
  setPendingAction,
  setStatusFilter,
  setError,
  setMainLaneIds,
  setViewLaneIds,
  setLaneTrees,
  primaryCommandTarget,
  sideCommandTarget,
  refreshLaneTree,
  setCommandFeedback,
  bumpConversationsRevision,
  openConversation,
  loadConversation,
  applyRuntimeSnapshot,
  dismissSideConversation,
  closeSideConversation,
}: ConversationLifecycleParams) {

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
      bumpConversationsRevision();
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
      bumpConversationsRevision();
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
  return {
    newConversation,
    createTemporaryConversation,
    promoteConversation,
    renameConversation,
    changeConversationModel,
    changeConversationStatus,
    deleteConversation,
  };
}
