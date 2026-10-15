import { chatApi } from "./api";
import { readableError } from "./apiErrorText";
import type {
  Conversation,
  ConversationSnapshot,
  LiveTurn,
  RuntimeV2ConversationReference,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";
import type { SnapshotTarget } from "./chatApplicationSupport";

/**
 * 消息与提案动作：发送、上传/移除附件、取消/转向、审批与恢复、变体重试与切换。
 *
 * 从 `useChatApplication.ts`（1 129 行）抽出——这是最后一个大簇（约 400 行，13 个成员），
 * 也是依赖面最宽的一个：它几乎用到所有上层 hook（运行时事件桥、侧栏、车道）与父层的
 * 全部工作面状态。
 *
 * **行为零改动**：整段逐行搬运（边界按源码人工确认：635 的 `const send` 到 1038 的 `};`），
 * 只把闭包里的量改成参数解构。
 */
export type MessageActionsParams = {
  runtimeController: ReturnType<typeof import("./runtimeController").useConversationRuntimeController>;
  activeConversationId: string | null;
  primaryCommandTarget: { conversationId: string; laneId: string | null } | null;
  sideCommandTarget: { conversationId: string; laneId: string | null } | null;
  activeRuntimeLaneId: string | null;
  sideLane: import("./chatApplicationSupport").SideLaneTarget | null;
  sideConversationId: string | null;
  sideDraft: string;
  sideIsGenerating: boolean;
  sideSnapshot: ConversationSnapshot | null;
  snapshots: Record<string, ConversationSnapshot>;
  liveTurns: Record<string, LiveTurn>;
  mainLaneIds: Record<string, string>;
  conversations: Conversation[];
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  setSnapshots: React.Dispatch<React.SetStateAction<Record<string, ConversationSnapshot>>>;
  setLiveTurns: React.Dispatch<React.SetStateAction<Record<string, LiveTurn>>>;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  setSideDraft: React.Dispatch<React.SetStateAction<string>>;
  setSideLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setError: React.Dispatch<React.SetStateAction<string | null>>;
  setFocusTarget: React.Dispatch<
    React.SetStateAction<{ conversationId: string; turnId: string; nonce: number } | null>
  >;
  setCommandFeedback: (
    target: { conversationId: string; laneId: string | null } | null,
    nextPendingAction: string | null,
    nextError: string | null,
  ) => void;
  openConversation: (
    conversationId: string,
    options?: { focusTurnId?: string | null },
  ) => Promise<void>;
  loadConversation: (conversationId: string) => Promise<void>;
  bumpConversationsRevision: () => void;
  applyRuntimeSnapshot: (
    legacy: ConversationSnapshot,
    runtime: RuntimeV2Snapshot,
    target?: SnapshotTarget,
  ) => void;
  followRuntimeConversation: (
    conversationId: string,
    initialSequence: number,
    laneId?: string | null,
    target?: SnapshotTarget,
  ) => void;
  hydrateActiveTurn: (
    legacy: ConversationSnapshot,
    target?: SnapshotTarget,
    isCurrent?: () => boolean,
  ) => Promise<void>;
  sideRequestVersion: React.MutableRefObject<number>;
  primarySurfaceRequestVersion: React.MutableRefObject<number>;
  viewLaneIds: Record<string, string>;
  setPendingAction: React.Dispatch<React.SetStateAction<string | null>>;
  setEditedUserMessages: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  draft: string;
  isGenerating: boolean;
  latestTurn: import("./apiTypes").ConversationTurnSnapshot | undefined;
  activeRunRunning: boolean;
  activeRunningRunId: string | null;
  sendRuntimeV2Message: (
    conversationId: string,
    content: string,
    laneId?: string | null,
    target?: SnapshotTarget,
    references?: RuntimeV2ConversationReference[],
  ) => Promise<unknown>;
};

export function useMessageActions({
  runtimeController,
  activeConversationId,
  primaryCommandTarget,
  sideCommandTarget,
  activeRuntimeLaneId,
  sideLane,
  sideConversationId,
  sideDraft,
  sideIsGenerating,
  sideSnapshot,
  snapshots,
  liveTurns,
  mainLaneIds,
  conversations,
  setConversations,
  setSnapshots,
  setLiveTurns,
  setDraft,
  setSideDraft,
  setSideLoading,
  setError,
  setFocusTarget,
  setCommandFeedback,
  openConversation,
  loadConversation,
  bumpConversationsRevision,
  applyRuntimeSnapshot,
  followRuntimeConversation,
  hydrateActiveTurn,
  sideRequestVersion,
  primarySurfaceRequestVersion,
  viewLaneIds,
  setPendingAction,
  setEditedUserMessages,
  draft,
  isGenerating,
  latestTurn,
  activeRunRunning,
  activeRunningRunId,
  sendRuntimeV2Message,
}: MessageActionsParams) {

  /**
   * C3b：`references` = composer 选中的 `@` 引用（判别联合数组）。
   *
   * 返回值（C3b 修复波 §6）：`true` = 消息**真的提交成功**（composer 据此清空 chip）；
   * `false` = 没发出去（草稿空 / 无会话 / 生成中 / 纠偏通道 / 请求失败）⇒ 调用方保留
   * 用户已选的引用，别让它重填。失败时照样把保留的草稿写回输入框（原有行为）。
   *
   * 纠偏（steer）通道**不带**引用：那条路是给在飞 run 注入指令，后端没有对应的
   * 引用通道（composer 在可纠偏时会明说"本次不生效"），这里不做静默伪装，也不把
   * 引用算作"已成功送出"。
   */
  const send = async (
    references: RuntimeV2ConversationReference[] = [],
  ): Promise<boolean> => {
    const content = draft.trim();
    if (!content || !activeConversationId) return false;
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
      return false;
    }
    const retainedDraft = draft;
    if (isGenerating) return false;
    setDraft("");
    setCommandFeedback(primaryCommandTarget, "send", null);
    try {
      await sendRuntimeV2Message(
        activeConversationId,
        content,
        viewLaneIds[activeConversationId] ?? mainLaneIds[activeConversationId],
        "main",
        references,
      );
      setCommandFeedback(primaryCommandTarget, null, null);
      return true;
    } catch (sendError) {
      setDraft(retainedDraft);
      setCommandFeedback(primaryCommandTarget, null, readableError(sendError));
      return false;
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
    // E3/B：`resume` = 在**同一条 run** 上接着跑（硬停终态原地复活）。
    // 三条动作共用既有恢复入口；后端按 run 行状态分派，拒绝时回 409
    // `code=run_not_revivable`（本函数把它翻成可读错误提示）。
    action: "retry" | "mark_failed" | "resume",
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

  return {
    send,
    uploadFile,
    removeFile,
    cancel,
    cancelRuntimeRun,
    steerRuntimeRun,
    resolveApproval,
    resolveRuntimeRecovery,
    runRuntimeV2Command,
    retry,
    regenerate,
    resend,
    selectVariant,
  };
}
