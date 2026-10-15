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
  RuntimeV2ConversationReference,
  RuntimeV2Entry,
  RuntimeV2Lane,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  Message,
  TurnStatus,
  Workspace,
} from "./apiTypes";
import { readableError } from "./apiErrorText";
import { useRuntimeEventBridge } from "./useRuntimeEventBridge";
import { useSideConversation } from "./useSideConversation";
import { useLaneActions } from "./useLaneActions";
import { useWorkspaceConversations } from "./useWorkspaceConversations";
import { useConversationLifecycle } from "./useConversationLifecycle";
import { useMessageActions } from "./useMessageActions";
import {
  runtimeTargetKey,
  useConversationRuntimeController,
} from "./runtimeController";

import {
  ACTIVE_CONVERSATION_STORAGE_KEY,
  VIEW_LANE_STORAGE_KEY,
  WORKSPACE_STORAGE_KEY,
  activeRuntimeStatuses,
  conversationIdFromHash,
  liveFromRuntimeSnapshot,
  readStoredActiveConversation,
  readStoredViewLanes,
  readStoredWorkspace,
  requestId,
  terminalStatuses,
  writeStoredViewLanes,
  type SideLaneTarget,
  type SnapshotTarget,
} from "./chatApplicationSupport";
import {
  runtimeSnapshotToConversationSnapshot,
  runtimeStatusToTurnStatus,
} from "./conversationSnapshotMapper";
export function useChatApplication() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  // 侧栏（SessionRail）走独立的数据源（useWorkspaceNavigation 按工作区并发拉取），
  // 所以"新建会话/会话改名"这类变化必须显式通知它刷新，否则侧栏要等下次整体加载。
  const [conversationsRevision, setConversationsRevision] = useState(0);
  // 供 loadConversationList 决策（见下方同步 effect）：避免列表刷新抢走用户当前所在会话。
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [initialHashId] = useState(conversationIdFromHash);
  const [initialHashPending, setInitialHashPending] = useState(Boolean(initialHashId));
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
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  // 通知/引用跳转：打开会话后需要滚动定位到的轮次（turnId = v2 run id）。
  const [focusTarget, setFocusTarget] = useState<
    { conversationId: string; turnId: string; nonce: number } | null
  >(null);
  const focusNonce = useRef(0);
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
  const primarySurfaceRequestVersion = useRef(0);
  // 列表簇要用：hashchange 路由与卸载清理也要用它们，故留在父层。
  const activeConversationIdRef = useRef<string | null>(null);
  const conversationsRef = useRef<Conversation[]>([]);
  const sideRequestVersion = useRef(0);
  const initialHashTarget = useRef<string | undefined>(undefined);
  const initialSelectionVersion = useRef(0);
  const cancelInitialHash = useCallback(() => {
    initialSelectionVersion.current += 1;
    initialHashTarget.current = undefined;
    setInitialHashPending(false);
  }, []);
  // 运行时事件桥：把 v2 运行时推来的快照/事件投影成界面状态。
  // 自己在外面建一次 runtimeController 并传进去——两边各建一份会出现两套 SSE 连接。
  const {
    applyRuntimeSnapshot,
    applyRuntimeProductEvent,
    followRuntimeConversation,
    hydrateActiveTurn,
    unbindRuntimeTarget,
  } = useRuntimeEventBridge({
    runtimeController,
    snapshots,
    sideSnapshots,
    viewLaneIds,
    primaryConversationId: activeConversationId,
    setSnapshots,
    setSideSnapshots,
    setLiveTurns,
    setConversations,
    setMainLaneIds,
    setViewLaneIds,
    setLaneTrees,
  });

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

  // 命令反馈的**唯一出口**：有 runtime target 时写进 controller（按会话/车道维度保存），
  // 否则退回主工作面的本地状态。包成 `useCallback` 是为了让它的身份稳定——侧栏簇与
  // 发送/变体/提案等回调都会把它列进依赖数组，普通函数会让那些数组每次渲染都失效。
  const setCommandFeedback = useCallback(
    (
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
    },
    [runtimeController.setCommand],
  );
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
    async (
      conversationId: string,
      options?: { focusTurnId?: string | null },
    ) => {
      cancelInitialHash();
      hasOpenedConversationRef.current = true;
      setActiveConversationId(conversationId);
      // 先清空再设置：同一轮次重复点击也要能重新滚动（用 nonce 触发 effect）。
      const focusTurnId = options?.focusTurnId ?? null;
      focusNonce.current += 1;
      setFocusTarget(
        focusTurnId
          ? { conversationId, turnId: focusTurnId, nonce: focusNonce.current }
          : null,
      );
      await loadConversation(conversationId);
    },
    [cancelInitialHash, loadConversation],
  );

  // 会话列表与工作区：只依赖 openConversation，故紧随其后调用。
  const {
    loadConversationList,
    refreshCapabilities,
    refreshProviders,
    visibleConversations,
    currentWorkspace,
    workspaceCanCreate,
    bumpConversationsRevision,
    selectWorkspace,
    createWorkspace,
    refreshWorkspaces,
    deleteWorkspace,
  } = useWorkspaceConversations({
    onSelectWorkspace: cancelInitialHash,
    conversationListVersion,
    activeConversationIdRef,
    conversationsRef,
    conversations,
    workspaces,
    homePath,
    workspaceId,
    search,
    statusFilter,
    setConversations,
    setConversationsRevision,
    setWorkspaces,
    setHomePath,
    setWorkspaceId,
    setActiveConversationId,
    setLoading,
    setError,
    setHealth,
    setCapabilities,
    setProviders,
    openConversation,
  });

  const sendRuntimeV2Message = async (
    conversationId: string,
    content: string,
    laneId?: string | null,
    target: SnapshotTarget = "main",
    // C3b：`@` 引用（判别联合数组）。缺省空 ⇒ 请求体与今天逐字相同。
    references: RuntimeV2ConversationReference[] = [],
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
      references,
    );
    const legacy = await chatApi.getConversation(conversationId);
    const runtime = await runtimeController.loadSnapshot({
      conversationId,
      laneId: result.laneId,
    });
    if (!isCurrent()) return result;
    applyRuntimeSnapshot(legacy, runtime, target);
    // 首条消息会给会话自动命名（"摘要式标题"，与后端同一规则）：把侧栏行同步刷新，
    // 否则列表要等下次整体加载才更新，用户看到的一直是「新对话」。
    setConversations((current) =>
      current.some((item) => item.id === conversationId)
        ? current.map((item) =>
            item.id === conversationId ? { ...item, ...legacy.conversation } : item,
          )
        : current,
    );
    bumpConversationsRevision();
    followRuntimeConversation(
      conversationId,
      before.lastEventSeq,
      result.laneId,
      target,
    );
    return result;
  };
  // 侧栏派生值（快照 / 最新轮 / 是否生成中）：侧栏会话簇要用，故先算。

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

  // 侧栏工作面（对照/分支）的会话簇：依赖上面的运行时事件桥，故在其后调用。
  const {
    openSideConversation,
    dismissSideConversation,
    focusTemporaryConversation,
    waitForRuntimeRunToStop,
    closeSideConversation,
    openLaneInSide,
    sendSide,
  } = useSideConversation({
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
  });

  // 车道路由动作：依赖运行时事件桥与侧栏的 openLaneInSide，故在两者之后调用。
  const {
    refreshLaneTree,
    switchLane,
    forkLane,
    promoteLane,
    renameLane,
    setLaneArchived,
    setArchivedLanesVisible,
  } = useLaneActions({
    runtimeController,
    mainLaneIds,
    viewLaneIds,
    snapshots,
    sideLane,
    setLaneTrees,
    setMainLaneIds,
    setViewLaneIds,
    applyRuntimeSnapshot,
    followRuntimeConversation,
    openLaneInSide,
    primarySurfaceRequestVersion,
    unbindRuntimeTarget,
    setPendingAction,
    setError,
    dismissSideConversation,
    loadConversation,
  });

  // 会话生命周期：依赖车道树的 refreshLaneTree 与列表的 loadConversationList，故在其后。
  const {
    newConversation: createNewConversation,
    createTemporaryConversation,
    promoteConversation,
    renameConversation,
    changeConversationModel,
    changeConversationStatus,
    deleteConversation,
  } = useConversationLifecycle({
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
  });

  const newConversation = (targetWorkspaceId?: string) => {
    cancelInitialHash();
    return createNewConversation(targetWorkspaceId);
  };








  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);

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
          if (initialHashTarget.current) return current;
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
    if (!initialHashId) return;
    let cancelled = false;
    const selectionVersion = initialSelectionVersion.current;
    const isCurrent = () =>
      !cancelled && initialSelectionVersion.current === selectionVersion;
    void chatApi
      .getConversation(initialHashId)
      .then((snapshot) => {
        if (!isCurrent()) return;
        const workspaceId = snapshot.conversation.workspaceId ?? null;
        initialHashTarget.current = initialHashId;
        setWorkspaceId(workspaceId);
        setStatusFilter(snapshot.conversation.status);
        try {
          globalThis.localStorage?.setItem(
            ACTIVE_CONVERSATION_STORAGE_KEY,
            JSON.stringify({ conversationId: initialHashId, workspaceId }),
          );
          globalThis.localStorage?.setItem(WORKSPACE_STORAGE_KEY, workspaceId ?? "general");
        } catch {
          // 忽略存储异常。
        }
      })
      .catch(() => {
        // 无效/已删除会话：静默回退到常规恢复路径。
      })
      .finally(() => {
        if (isCurrent()) setInitialHashPending(false);
      });
    return () => {
      cancelled = true;
    };
  }, [initialHashId]);

  useEffect(() => {
    // 先解析显式 URL 的会话/工作区，再允许列表选择默认会话。
    if (initialHashPending) return;
    // 刷新恢复上次会话：activeConversationId 尚未恢复时，用已持久化的会话 id 作为首选目标。
    const restored = activeConversationId
      ? activeConversationId
      : (initialHashTarget.current ?? readStoredActiveConversation()?.conversationId ?? undefined);
    void loadConversationList(statusFilter, restored);
    // active id 不作为重载触发；切换工作区时列表整体换防。
  }, [initialHashPending, statusFilter, workspaceId, workspaceCanCreate]);

  useEffect(() => {
    // 主线变化即同步 URL（replaceState，不产生历史项）；临时/遗留 ephemeral 不写。
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

  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);
  useEffect(() => {
    conversationsRef.current = conversations;
  }, [conversations]);

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


  // 消息与提案动作：最后一个大簇，依赖上面的运行时事件桥、侧栏、车道与列表 hook。
  const {
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
  } = useMessageActions({
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
  });

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
    conversationsRevision,
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
    focusTarget,
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
