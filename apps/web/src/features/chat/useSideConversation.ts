import { useCallback, type MutableRefObject } from "react";
import { chatApi } from "./api";
import { readableError } from "./apiErrorText";
import type {
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  RuntimeV2Snapshot,
  RuntimeV2Lane,
} from "./apiTypes";
import type { SideLaneTarget } from "./chatApplicationSupport";
import {
  runtimeTargetKey,
  useConversationRuntimeController,
} from "./runtimeController";

/**
 * 侧栏工作面（右侧对照/分支）的会话簇。
 *
 * 从 `useChatApplication.ts`（1 806 行）抽出，共 222 行：打开/关闭/丢弃侧栏会话、
 * 聚焦临时会话、等待运行停止、在侧栏打开某条车道、以及侧栏发送。
 *
 * **拆法要点**：这一簇**依赖运行时事件簇**（`applyRuntimeSnapshot` /
 * `followRuntimeConversation` / `hydrateActiveTurn` + `runtimeController`），所以顺序上
 * 必须先有 `useRuntimeEventBridge`（上一轮已完成）——反序会把两簇搅在一起。
 * 侧栏自己的状态（`sideLane` / `sideConversationId` / `sideDraft`）仍留在父层，
 * 这里只接参数与 setter，避免搬动 `useState` 顺序（那会改变 effect 的时序）。
 *
 * 行为零改动：七个成员逐行原样搬运，只把闭包里的量改成参数解构。
 */
export type SideConversationParams = {
  runtimeController: ReturnType<typeof useConversationRuntimeController>;
  /** 侧栏导航的请求版本守卫（并发打开时旧响应不得覆盖新状态）。 */
  sideRequestVersion: MutableRefObject<number>;
  sideLane: SideLaneTarget | null;
  sideConversationId: string | null;
  sideDraft: string;
  sideCommandTarget: { conversationId: string; laneId: string | null } | null;
  mainLaneIds: Record<string, string>;
  setSnapshots: React.Dispatch<
    React.SetStateAction<Record<string, ConversationSnapshot>>
  >;
  setSideSnapshots: React.Dispatch<
    React.SetStateAction<Record<string, ConversationSnapshot>>
  >;
  setSideConversationId: React.Dispatch<React.SetStateAction<string | null>>;
  setSideLane: React.Dispatch<React.SetStateAction<SideLaneTarget | null>>;
  setSideDraft: React.Dispatch<React.SetStateAction<string>>;
  setSideLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  setMainLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setViewLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setLaneTrees: React.Dispatch<
    React.SetStateAction<Record<string, RuntimeV2Lane[]>>
  >;
  /** 命令反馈出口（父层的 `setCommandFeedback`，已提成 `useCallback`）。 */
  setCommandFeedback: (
    target: { conversationId: string; laneId: string | null } | null,
    nextPendingAction: string | null,
    nextError: string | null,
  ) => void;
  openConversation: (
    conversationId: string,
    options?: { focusTurnId?: string | null },
  ) => Promise<void>;
  hydrateActiveTurn: (
    legacy: ConversationSnapshot,
    target?: "main" | "side",
    isCurrent?: () => boolean,
  ) => Promise<void>;
  applyRuntimeSnapshot: (
    legacy: ConversationSnapshot,
    runtime: RuntimeV2Snapshot,
    target?: "main" | "side",
  ) => void;
  followRuntimeConversation: (
    conversationId: string,
    initialSequence: number,
    laneId?: string | null,
    target?: "main" | "side",
  ) => void;
  unbindRuntimeTarget: (
    target: { conversationId: string; laneId: string | null },
    surface: "main" | "side",
  ) => void;
  /** 关闭侧栏后重建会话列表时沿用的过滤条件。 */
  statusFilter: ConversationStatus;
  workspaceId: string | null;
  /** 侧栏当前是否正在生成（发送前的守卫）。 */
  sideIsGenerating: boolean;
  /** 提交消息并驱动运行（父层实现，侧栏与主工作面共用）。 */
  sendRuntimeV2Message: (
    conversationId: string,
    content: string,
    laneId?: string | null,
    target?: "main" | "side",
  ) => Promise<unknown>;
};

export function useSideConversation({
  runtimeController,
  sideRequestVersion,
  sideLane,
  sideConversationId,
  sideDraft,
  sideCommandTarget,
  mainLaneIds,
  setSnapshots,
  setSideSnapshots,
  setSideConversationId,
  setSideLane,
  setSideDraft,
  setSideLoading,
  setConversations,
  setMainLaneIds,
  setViewLaneIds,
  setLaneTrees,
  setCommandFeedback,
  openConversation,
  hydrateActiveTurn,
  applyRuntimeSnapshot,
  followRuntimeConversation,
  unbindRuntimeTarget,
  statusFilter,
  workspaceId,
  sideIsGenerating,
  sendRuntimeV2Message,
}: SideConversationParams) {

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

  return {
    openSideConversation,
    dismissSideConversation,
    focusTemporaryConversation,
    waitForRuntimeRunToStop,
    closeSideConversation,
    openLaneInSide,
    sendSide,
  };
}
