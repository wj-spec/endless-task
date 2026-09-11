import { useCallback, type MutableRefObject } from "react";
import { chatApi } from "./api";
import { readableError } from "./apiErrorText";
import type {
  ConversationSnapshot,
  RuntimeV2Lane,
  RuntimeV2Snapshot,
} from "./apiTypes";
import type { SideLaneTarget } from "./chatApplicationSupport";
import type { useConversationRuntimeController } from "./runtimeController";

/**
 * 车道路由动作：刷新车道树、切换/分叉/提升/重命名车道、归档与显示已归档。
 *
 * 从 `useChatApplication.ts`（1 638 行）抽出，共 220 行。这一簇的边界很清楚——它只
 * 操作"车道（lane）"这一层，依赖的 13 个外部量全部来自运行时事件桥（
 * `applyRuntimeSnapshot` / `followRuntimeConversation`）、侧栏簇（`openLaneInSide`）
 * 与父层的车道状态/setter。
 *
 * 行为零改动：七个成员逐行原样搬运，只把闭包里的量改成参数解构。
 */
export type LaneActionsParams = {
  runtimeController: ReturnType<typeof useConversationRuntimeController>;
  /** `mainLaneIds` / `viewLaneIds`：会话 → 主线 / 当前查看车道的映射。 */
  mainLaneIds: Record<string, string>;
  viewLaneIds: Record<string, string>;
  /** 临时会话快照（分叉/提升时作为基线）。 */
  snapshots: Record<string, ConversationSnapshot>;
  sideLane: SideLaneTarget | null;
  setLaneTrees: React.Dispatch<
    React.SetStateAction<Record<string, RuntimeV2Lane[]>>
  >;
  setMainLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setViewLaneIds: React.Dispatch<React.SetStateAction<Record<string, string>>>;
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
  /** 在侧栏打开某条车道（侧栏簇提供）。 */
  openLaneInSide: (conversationId: string, laneId: string) => Promise<void>;
  /** 切换车道时的请求版本守卫（并发切换时旧响应不得覆盖新状态）。 */
  primarySurfaceRequestVersion: MutableRefObject<number>;
  /** 解绑某条车道的运行时订阅（运行时事件桥提供）。 */
  unbindRuntimeTarget: (
    target: { conversationId: string; laneId: string | null },
    surface: "main" | "side",
  ) => void;
  setPendingAction: React.Dispatch<React.SetStateAction<string | null>>;
  setError: React.Dispatch<React.SetStateAction<string | null>>;
  /** 关闭侧栏（侧栏簇提供；切换车道前要先收起侧栏）。 */
  dismissSideConversation: (preserveTemporary?: boolean) => void;
  /** 重新载入主工作面的会话（父层提供）。 */
  loadConversation: (conversationId: string) => Promise<void>;
};

export function useLaneActions({
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
}: LaneActionsParams) {

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
      const requestVersion = primarySurfaceRequestVersion.current + 1;
      primarySurfaceRequestVersion.current = requestVersion;
      setPendingAction("switch-lane");
      setError(null);
      try {
        const previousLaneId =
          viewLaneIds[conversationId] ?? mainLaneIds[conversationId] ?? null;
        if (previousLaneId && previousLaneId !== laneId) {
          unbindRuntimeTarget(
            { conversationId, laneId: previousLaneId },
            "main",
          );
        }
        setViewLaneIds((current) => ({
          ...current,
          [conversationId]: laneId,
        }));
        const [legacy, runtime] = await Promise.all([
          chatApi.getConversation(conversationId),
          runtimeController.loadSnapshot({ conversationId, laneId }),
        ]);
        if (primarySurfaceRequestVersion.current !== requestVersion) {
          unbindRuntimeTarget({ conversationId, laneId }, "main");
          return;
        }
        applyRuntimeSnapshot(legacy, runtime);
        followRuntimeConversation(conversationId, runtime.lastEventSeq, laneId);
      } catch (loadError) {
        if (primarySurfaceRequestVersion.current === requestVersion) {
          setError(readableError(loadError));
        }
      } finally {
        if (primarySurfaceRequestVersion.current === requestVersion) {
          setPendingAction(null);
        }
      }
    },
    [
      applyRuntimeSnapshot,
      followRuntimeConversation,
      mainLaneIds,
      runtimeController.loadSnapshot,
      unbindRuntimeTarget,
      viewLaneIds,
    ],
  );

  const forkLane = useCallback(
    async (
      conversationId: string,
      sourceLaneId: string | null | undefined,
      forkTurnId?: string,
    ) => {
      setPendingAction("fork-lane");
      setError(null);
      try {
        if (!forkTurnId) {
          throw new Error("请从一条已完成的回答创建分支。");
        }
        const turnSnapshot = snapshots[conversationId]?.turns.find(
          (item) => item.turn.id === forkTurnId,
        );
        const baseEntryId = turnSnapshot?.responseVariants.find(
          (item) => item.variant.id === turnSnapshot.turn.activeResponseVariantId,
        )?.assistantMessage.id;
        if (!baseEntryId || turnSnapshot?.turn.status !== "completed") {
          throw new Error("只能从完整回答结束处创建分支。");
        }
        const resolvedSourceLaneId =
          sourceLaneId ?? viewLaneIds[conversationId] ?? mainLaneIds[conversationId];
        if (!resolvedSourceLaneId) {
          throw new Error("当前会话暂不支持创建分支，请确认会话已加载主线。");
        }
        const created = await chatApi.createRuntimeV2Lane(conversationId, {
          sourceLaneId: resolvedSourceLaneId,
          baseEntryId,
        });
        await refreshLaneTree(conversationId);
        await openLaneInSide(conversationId, created.lane.id);
      } catch (forkError) {
        setError(readableError(forkError));
      } finally {
        setPendingAction(null);
      }
    },
    [mainLaneIds, openLaneInSide, refreshLaneTree, snapshots, viewLaneIds],
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
              runtimeController.loadSnapshot({
                conversationId,
                laneId: nextLaneId,
              }),
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

  return {
    refreshLaneTree,
    switchLane,
    forkLane,
    promoteLane,
    renameLane,
    setLaneArchived,
    setArchivedLanesVisible,
  };
}
