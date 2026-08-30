import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatApi,
  streamRuntimeV2Events,
} from "../chat/api";
import type {
  RuntimeV2Lane,
  RuntimeV2ConversationRuntimeStatus,
  RuntimeV2Memory,
  RuntimeV2MemoryPromotion,
  RuntimeV2MemoryPromotionTarget,
  RuntimeV2ProductEvent,
  RuntimeV2RunVariant,
  RuntimeV2Snapshot,
} from "../chat/apiTypes";
import { StatusBadge } from "../ui/StatusBadge";

const ACTIVE_RUN_STATUSES = new Set([
  "created",
  "queued",
  "running",
  "waiting_approval",
  "compacting",
  "cancelling",
]);

type RuntimeStatusPresentation = {
  label: string;
  tone: "neutral" | "active" | "warning" | "danger" | "success";
  pulse: boolean;
};

const runtimeStatusPresentation = (
  status: string | undefined,
): RuntimeStatusPresentation => {
  if (status === "created" || status === "queued") {
    return { label: "准备运行", tone: "active", pulse: true };
  }
  if (status === "running") {
    return { label: "运行中", tone: "active", pulse: true };
  }
  if (status === "waiting_approval") {
    return { label: "等待审批", tone: "warning", pulse: false };
  }
  if (status === "compacting") {
    return { label: "整理上下文", tone: "active", pulse: true };
  }
  if (status === "cancelling") {
    return { label: "正在取消", tone: "warning", pulse: true };
  }
  if (status === "completed") {
    return { label: "已完成", tone: "success", pulse: false };
  }
  if (status === "failed") {
    return { label: "运行失败", tone: "danger", pulse: false };
  }
  if (status === "cancelled") {
    return { label: "已取消", tone: "neutral", pulse: false };
  }
  return { label: "空闲", tone: "neutral", pulse: false };
};

const entryText: Record<string, string> = {
  user_message: "用户",
  assistant_message: "助手",
  tool_call: "工具调用",
  tool_result: "工具结果",
  context_summary: "上下文摘要",
};

type RuntimeV2PanelProps = {
  conversationId: string | null;
};

export function RuntimeV2Panel({ conversationId }: RuntimeV2PanelProps) {
  const [snapshot, setSnapshot] = useState<RuntimeV2Snapshot | null>(null);
  const [lanes, setLanes] = useState<RuntimeV2Lane[]>([]);
  const [runVariants, setRunVariants] = useState<RuntimeV2RunVariant[]>([]);
  const [runtimeStatus, setRuntimeStatus] =
    useState<RuntimeV2ConversationRuntimeStatus | null>(null);
  const [memories, setMemories] = useState<RuntimeV2Memory[]>([]);
  const [memoryPromotions, setMemoryPromotions] = useState<
    RuntimeV2MemoryPromotion[]
  >([]);
  const [activeLaneId, setActiveLaneId] = useState<string | null>(null);
  const [selectedLaneId, setSelectedLaneId] = useState<string | null>(null);
  const [events, setEvents] = useState<RuntimeV2ProductEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [memoryKind, setMemoryKind] = useState<"fact" | "preference">("fact");
  const [memoryPromotionTarget, setMemoryPromotionTarget] =
    useState<RuntimeV2MemoryPromotionTarget>("conversation_tree");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const streamRef = useRef<AbortController | null>(null);
  const cursorRef = useRef(0);

  const stopStream = useCallback(() => {
    streamRef.current?.abort();
    streamRef.current = null;
    setStreaming(false);
  }, []);

  const refreshLanes = useCallback(async (targetConversationId: string) => {
    const laneList = await chatApi.listRuntimeV2Lanes(targetConversationId, true);
    setLanes(laneList.items);
    setActiveLaneId(laneList.activeLaneId);
    return laneList;
  }, []);

  const refreshMemoryData = useCallback(
    async (
      targetConversationId: string,
      targetLaneId: string,
      runId?: string | null,
    ) => {
      const [memoryList, promotionList] = await Promise.all([
        chatApi.listRuntimeV2Memories(
          targetConversationId,
          targetLaneId,
          runId,
        ),
        chatApi.listRuntimeV2MemoryPromotions(targetConversationId),
      ]);
      setMemories(memoryList.items);
      setMemoryPromotions(promotionList.items);
    },
    [],
  );

  const applyProductEvent = useCallback((event: RuntimeV2ProductEvent) => {
    setEvents((current) => [...current, event].slice(-200));
    setSnapshot((current) => {
      if (!current) return current;
      const next: RuntimeV2Snapshot = {
        ...current,
        lastEventSeq: Math.max(current.lastEventSeq, event.eventSeq),
      };

      if (
        event.type === "message.updated" &&
        next.runState?.runId === event.runId
      ) {
        next.runState = {
          ...next.runState,
          partialContent: next.runState.partialContent + (event.data.delta ?? ""),
        };
      }
      if (
        (event.type === "run.status_changed" ||
          event.type === "run.finished" ||
          event.type === "run.failed" ||
          event.type === "run.cancelled") &&
        next.runState?.runId === event.runId
      ) {
        const status =
          event.type === "run.finished"
            ? "completed"
            : event.type === "run.failed"
              ? "failed"
              : event.type === "run.cancelled"
                ? "cancelled"
                : event.data.status;
        if (status) next.runState = { ...next.runState, status };
      }
      if (event.type === "approval.requested" && event.data.approvalId) {
        next.pendingApprovals = [
          ...next.pendingApprovals.filter(
            (approval) => approval.id !== event.data.approvalId,
          ),
          {
            id: event.data.approvalId,
            runId: event.runId ?? "",
            modelTurnId: event.data.modelTurnId ?? "",
            toolExecutionId: event.data.toolExecutionId ?? "",
            toolName: String(event.data.toolName ?? "工具"),
            summary: String(event.data.summary ?? "等待工具审批"),
            reason: String(event.data.reason ?? ""),
          },
        ];
      }
      if (event.type === "approval.resolved" && event.data.approvalId) {
        next.pendingApprovals = next.pendingApprovals.filter(
          (approval) => approval.id !== event.data.approvalId,
        );
      }
      if (
        event.type.startsWith("tool_execution.") &&
        event.data.toolExecutionId &&
        event.data.status
      ) {
        next.toolStates = next.toolStates.map((tool) =>
          tool.id === event.data.toolExecutionId
            ? { ...tool, status: String(event.data.status) }
            : tool,
        );
      }
      return next;
    });
  }, []);

  const follow = useCallback(
    async (targetConversationId: string, afterSequence: number) => {
      streamRef.current?.abort();
      const controller = new AbortController();
      streamRef.current = controller;
      cursorRef.current = afterSequence;
      setStreaming(true);
      try {
        await streamRuntimeV2Events({
          conversationId: targetConversationId,
          afterSequence,
          signal: controller.signal,
          onSnapshot: (nextSnapshot) => {
            cursorRef.current = Math.max(
              cursorRef.current,
              nextSnapshot.lastEventSeq,
            );
            setSnapshot(nextSnapshot);
          },
          onProductEvent: (event) => {
            cursorRef.current = Math.max(cursorRef.current, event.eventSeq);
            applyProductEvent(event);
          },
        });
      } catch (streamError) {
        if (!controller.signal.aborted) {
          setError(
            streamError instanceof Error
              ? streamError.message
              : "Runtime v2 事件流连接失败。",
          );
        }
      } finally {
        if (streamRef.current === controller) {
          streamRef.current = null;
          setStreaming(false);
        }
        if (!controller.signal.aborted) {
          try {
            const nextSnapshot = await chatApi.getRuntimeV2Snapshot(
              targetConversationId,
            );
            cursorRef.current = Math.max(
              cursorRef.current,
              nextSnapshot.lastEventSeq,
            );
            setSnapshot(nextSnapshot);
          } catch {
            // 结束态快照刷新失败时保留事件流状态。
          }
        }
      }
    },
    [applyProductEvent],
  );

  useEffect(() => {
    if (!conversationId) {
      setSnapshot(null);
      setRuntimeStatus(null);
      setLanes([]);
      setRunVariants([]);
      setMemories([]);
      setMemoryPromotions([]);
      setActiveLaneId(null);
      setSelectedLaneId(null);
      setEvents([]);
      setError(null);
      return;
    }
    setSnapshot(null);
    setRuntimeStatus(null);
    setLanes([]);
    setRunVariants([]);
    setMemories([]);
    setMemoryPromotions([]);
    setActiveLaneId(null);
    setSelectedLaneId(null);
    setEvents([]);
    setError(null);
    let cancelled = false;
    void (async () => {
      try {
        const nextRuntimeStatus =
          await chatApi.getRuntimeV2ConversationRuntimeStatus(conversationId);
        setRuntimeStatus(nextRuntimeStatus);
        const initialSnapshot = await chatApi.getRuntimeV2Snapshot(conversationId);
        const laneList = await refreshLanes(conversationId);
        if (cancelled) return;
        setSnapshot(initialSnapshot);
        setSelectedLaneId(laneList.activeLaneId);
        cursorRef.current = initialSnapshot.lastEventSeq;
        await follow(conversationId, initialSnapshot.lastEventSeq);
      } catch (loadError) {
        if (!cancelled) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "无法加载 Runtime v2 快照。",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
      stopStream();
    };
  }, [conversationId, follow, refreshLanes, stopStream]);

  const switchRuntime = async (runtime: "v1" | "v2") => {
    if (!conversationId || busy) return;
    setBusy(`runtime:${runtime}`);
    setError(null);
    try {
      const nextRuntimeStatus =
        await chatApi.setRuntimeV2ConversationRuntime(conversationId, runtime);
      setRuntimeStatus(nextRuntimeStatus);
    } catch (runtimeError) {
      setError(
        runtimeError instanceof Error
          ? runtimeError.message
          : "切换 Runtime 失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const activeRun = snapshot?.runState ?? null;
  const activeRunPresentation = runtimeStatusPresentation(activeRun?.status);
  const isActiveRun =
    !!activeRun && ACTIVE_RUN_STATUSES.has(activeRun.status);
  const selectedLane =
    lanes.find((lane) => lane.id === selectedLaneId) ?? null;

  useEffect(() => {
    const variantRunId =
      snapshot?.activeRunVariantId ?? snapshot?.activeRunId ?? null;
    if (!conversationId || !variantRunId) {
      setRunVariants([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const response = await chatApi.listRuntimeV2RunVariants(variantRunId);
        if (!cancelled) setRunVariants(response.items);
      } catch {
        if (!cancelled) setRunVariants([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, snapshot?.activeRunId, snapshot?.activeRunVariantId]);

  useEffect(() => {
    if (!conversationId || !selectedLaneId) {
      setMemories([]);
      setMemoryPromotions([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        await refreshMemoryData(
          conversationId,
          selectedLaneId,
          snapshot?.activeRunId,
        );
      } catch {
        if (!cancelled) {
          setMemories([]);
          setMemoryPromotions([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    conversationId,
    refreshMemoryData,
    selectedLaneId,
    snapshot?.activeRunId,
  ]);

  const submit = async () => {
    const content = draft.trim();
    if (!conversationId || !content || busy) return;
    setBusy(isActiveRun ? "steer" : "send");
    setError(null);
    try {
      if (activeRun) {
        await chatApi.steerRuntimeV2Run(activeRun.runId, content);
      } else {
        const result = await chatApi.createRuntimeV2Message(
          conversationId,
          content,
          selectedLaneId,
        );
        await refreshLanes(conversationId);
        setSelectedLaneId(result.laneId);
      }
      setDraft("");
      await follow(conversationId, cursorRef.current);
    } catch (submitError) {
      setError(
        submitError instanceof Error ? submitError.message : "Runtime v2 请求失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const createBranch = async () => {
    if (!conversationId || busy || isActiveRun) return;
    const sourceLaneId = selectedLaneId ?? activeLaneId;
    if (!sourceLaneId) {
      setError("当前会话还没有可分支的 Runtime v2 lane。");
      return;
    }
    setBusy("branch:persistent");
    setError(null);
    try {
      const result = await chatApi.createRuntimeV2Lane(conversationId, {
        sourceLaneId,
      });
      setSelectedLaneId(result.lane.id);
      await refreshLanes(conversationId);
      await follow(conversationId, cursorRef.current);
    } catch (branchError) {
      setError(
        branchError instanceof Error ? branchError.message : "创建分支失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const promoteSelectedLane = async () => {
    if (!conversationId || !selectedLane || busy || isActiveRun) return;
    if (selectedLane.isMain || selectedLane.archived) return;
    setBusy(`promote:${selectedLane.id}`);
    setError(null);
    try {
      await chatApi.promoteRuntimeV2Lane(selectedLane.id);
      const laneList = await refreshLanes(conversationId);
      setSelectedLaneId(laneList.activeLaneId);
      await follow(conversationId, cursorRef.current);
    } catch (promoteError) {
      setError(
        promoteError instanceof Error ? promoteError.message : "设置主线失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const setSelectedLaneArchived = async (archived: boolean) => {
    if (!conversationId || !selectedLane || busy || isActiveRun) return;
    if (archived && selectedLane.isMain) return;
    setBusy(`${archived ? "archive" : "restore"}:${selectedLane.id}`);
    setError(null);
    try {
      if (archived) {
        await chatApi.archiveRuntimeV2Lane(selectedLane.id);
      } else {
        await chatApi.restoreRuntimeV2Lane(selectedLane.id);
      }
      const laneList = await refreshLanes(conversationId);
      setSelectedLaneId(
        archived ? laneList.activeLaneId : selectedLane.id,
      );
      await follow(conversationId, cursorRef.current);
    } catch (archiveError) {
      setError(
        archiveError instanceof Error
          ? archiveError.message
          : archived
            ? "归档分支失败。"
            : "恢复分支失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const regenerateActiveVariant = async () => {
    const variantRunId =
      snapshot?.activeRunVariantId ?? snapshot?.activeRunId ?? null;
    if (!conversationId || !variantRunId || busy || isActiveRun) return;
    setBusy(`regenerate:${variantRunId}`);
    setError(null);
    try {
      await chatApi.regenerateRuntimeV2Run(variantRunId);
      await follow(conversationId, cursorRef.current);
    } catch (regenerateError) {
      setError(
        regenerateError instanceof Error
          ? regenerateError.message
          : "重新生成 Runtime v2 失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const selectRunVariant = async (runId: string) => {
    if (!conversationId || busy || isActiveRun) return;
    setBusy(`variant:${runId}`);
    setError(null);
    try {
      await chatApi.selectRuntimeV2RunVariant(runId);
      const nextSnapshot = await chatApi.getRuntimeV2Snapshot(conversationId);
      setSnapshot(nextSnapshot);
      cursorRef.current = nextSnapshot.lastEventSeq;
      await follow(conversationId, nextSnapshot.lastEventSeq);
    } catch (selectError) {
      setError(
        selectError instanceof Error ? selectError.message : "切换 RunVariant 失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const createMemory = async () => {
    const content = memoryDraft.trim();
    if (!conversationId || !selectedLaneId || !content || busy) return;
    setBusy("memory:create");
    setError(null);
    try {
      await chatApi.createRuntimeV2Memory(
        conversationId,
        selectedLaneId,
        memoryKind,
        content,
      );
      setMemoryDraft("");
      await refreshMemoryData(
        conversationId,
        selectedLaneId,
        snapshot?.activeRunId,
      );
      await follow(conversationId, cursorRef.current);
    } catch (memoryError) {
      setError(
        memoryError instanceof Error ? memoryError.message : "创建记忆失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const createMemoryPromotion = async (memoryId: string) => {
    if (!conversationId || !selectedLaneId || busy) return;
    setBusy(`memory-promotion:${memoryId}`);
    setError(null);
    try {
      const targetLaneId =
        memoryPromotionTarget === "branch" &&
        selectedLane?.kind === "temporary"
          ? selectedLane.sourceLaneId
          : null;
      if (memoryPromotionTarget === "branch" && !targetLaneId) {
        setError("临时会话需要一个持久父分支作为 Branch 记忆提升目标。");
        return;
      }
      await chatApi.createRuntimeV2MemoryPromotion(
        memoryId,
        memoryPromotionTarget,
        targetLaneId,
      );
      await refreshMemoryData(
        conversationId,
        selectedLaneId,
        snapshot?.activeRunId,
      );
      await follow(conversationId, cursorRef.current);
    } catch (promotionError) {
      setError(
        promotionError instanceof Error
          ? promotionError.message
          : "创建记忆提升提案失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const resolveMemoryPromotion = async (
    promotionId: string,
    decision: "accept" | "reject",
  ) => {
    if (!conversationId || !selectedLaneId || busy) return;
    setBusy(`memory-promotion-resolve:${promotionId}`);
    setError(null);
    try {
      await chatApi.resolveRuntimeV2MemoryPromotion(promotionId, decision);
      await refreshMemoryData(
        conversationId,
        selectedLaneId,
        snapshot?.activeRunId,
      );
      await follow(conversationId, cursorRef.current);
    } catch (resolveError) {
      setError(
        resolveError instanceof Error ? resolveError.message : "记忆提升决策失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const cancelRun = async () => {
    if (!activeRun || busy) return;
    setBusy("cancel");
    setError(null);
    try {
      await chatApi.cancelRuntimeV2Run(activeRun.runId);
      await follow(conversationId ?? "", cursorRef.current);
    } catch (cancelError) {
      setError(
        cancelError instanceof Error ? cancelError.message : "取消 Runtime v2 失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const resolveApproval = async (
    approvalId: string,
    decision: "approve" | "deny",
  ) => {
    if (busy) return;
    setBusy(`approval:${approvalId}`);
    setError(null);
    try {
      await chatApi.resolveRuntimeV2Approval(approvalId, decision);
      await follow(conversationId ?? "", cursorRef.current);
    } catch (approvalError) {
      setError(
        approvalError instanceof Error
          ? approvalError.message
          : "Runtime v2 审批失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  const resolveRecovery = async (
    runId: string,
    action: "mark_failed" | "retry",
  ) => {
    if (busy) return;
    setBusy(`recovery:${runId}`);
    setError(null);
    try {
      await chatApi.resolveRuntimeV2Recovery(runId, action);
      const nextSnapshot = await chatApi.getRuntimeV2Snapshot(
        conversationId ?? "",
      );
      setSnapshot(nextSnapshot);
      cursorRef.current = nextSnapshot.lastEventSeq;
      await follow(conversationId ?? "", nextSnapshot.lastEventSeq);
    } catch (recoveryError) {
      setError(
        recoveryError instanceof Error
          ? recoveryError.message
          : "Runtime v2 恢复决策失败。",
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-label="执行状态" className="runtime-v2-panel">
      <header className="runtime-v2-header">
        <div>
          <h3>执行状态</h3>
          <p>
            {conversationId
              ? streaming
                ? "事件流已连接"
                : "事件流待触发"
              : "请先选择会话"}
          </p>
        </div>
        <StatusBadge
          label={activeRunPresentation.label}
          pulse={activeRunPresentation.pulse}
          tone={activeRunPresentation.tone}
        />
      </header>

      {error ? (
        <div className="inline-error" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} type="button">
            关闭
          </button>
        </div>
      ) : null}

      {runtimeStatus ? (
        <div className="runtime-v2-runtime">
          <div>
            <strong>Runtime {runtimeStatus.effectiveRuntime}</strong>
            <p>
              {runtimeStatus.rollbackForced
                ? "全局 v1 回滚已启用"
                : runtimeStatus.rollbackReconciliationRequired
                  ? "回滚后产生 v1 增量，需先 reconciliation"
                : runtimeStatus.v1ReadOnly
                  ? "v1 数据已迁移归档为只读"
                : runtimeStatus.requiresMigration
                  ? "该会话尚未迁移到 v2"
                  : runtimeStatus.reason}
            </p>
          </div>
          <button
            disabled={
              busy !== null ||
              runtimeStatus.rollbackForced ||
              runtimeStatus.v1ReadOnly ||
              runtimeStatus.requiresMigration
            }
            onClick={() =>
              void switchRuntime(
                runtimeStatus.effectiveRuntime === "v2" ? "v1" : "v2",
              )
            }
            type="button"
          >
            {runtimeStatus.effectiveRuntime === "v2" ? "切回 v1" : "切到 v2"}
          </button>
        </div>
      ) : null}

      {snapshot ? (
        <>
          <div className="runtime-v2-metrics">
            <span>事件 {snapshot.lastEventSeq}</span>
            <span>
              Tokens {snapshot.contextUsage.inputTokens}/
              {snapshot.contextUsage.outputTokens}
            </span>
            <span>工具 {snapshot.toolStates.length}</span>
          </div>

          <div className="runtime-v2-lanes">
            <div className="runtime-v2-lane-toolbar">
              <strong>Lanes</strong>
              <div className="runtime-v2-actions">
                <button
                  disabled={busy !== null || isActiveRun || !activeLaneId}
                  onClick={() => void createBranch()}
                  type="button"
                >
                  创建分支
                </button>
                <button
                  disabled={
                    busy !== null ||
                    isActiveRun ||
                    !selectedLane ||
                    selectedLane.isMain ||
                    selectedLane.archived
                  }
                  onClick={() => void promoteSelectedLane()}
                  type="button"
                >
                  设为主线
                </button>
                <button
                  disabled={
                    busy !== null ||
                    isActiveRun ||
                    !selectedLane ||
                    selectedLane.isMain
                  }
                  onClick={() =>
                    void setSelectedLaneArchived(!selectedLane?.archived)
                  }
                  type="button"
                >
                  {selectedLane?.archived ? "恢复分支" : "归档分支"}
                </button>
              </div>
            </div>
            {lanes.map((lane) => (
              <div
                className={
                  lane.id === selectedLaneId ? "runtime-v2-lane is-selected" : "runtime-v2-lane"
                }
                key={lane.id}
              >
                <button
                  onClick={() => setSelectedLaneId(lane.id)}
                  type="button"
                >
                  <span>
                    {lane.isMain
                      ? "主线"
                      : lane.archived
                        ? "已归档分支"
                        : "持久分支"}
                  </span>
                  <strong>{lane.title ?? lane.id}</strong>
                </button>
              </div>
            ))}
          </div>

          <div className="runtime-v2-memory">
            <div className="runtime-v2-lane-toolbar">
              <strong>Memory Scopes</strong>
              <span>{selectedLane?.kind ?? "none"}</span>
            </div>
            <div className="runtime-v2-memory-list">
              {memories.map((memory) => (
                <div key={memory.id}>
                  <span>{memory.scope}</span>
                  <p>{memory.content}</p>
                  {memory.scope === "branch" ||
                  memory.scope === "temporary" ||
                  memory.scope === "run_scratch" ? (
                    <button
                      disabled={busy !== null}
                      onClick={() => void createMemoryPromotion(memory.id)}
                      type="button"
                    >
                      提升提案
                    </button>
                  ) : null}
                </div>
              ))}
              {memories.length === 0 ? (
                <p className="runtime-v2-empty">当前 lane 暂无可见记忆。</p>
              ) : null}
            </div>
            <div className="runtime-v2-memory-form">
              <select
                aria-label="记忆类型"
                onChange={(event) =>
                  setMemoryKind(event.target.value as "fact" | "preference")
                }
                value={memoryKind}
              >
                <option value="fact">Fact</option>
                <option value="preference">Preference</option>
              </select>
              <select
                aria-label="记忆提升目标"
                onChange={(event) =>
                  setMemoryPromotionTarget(
                    event.target.value as RuntimeV2MemoryPromotionTarget,
                  )
                }
                value={memoryPromotionTarget}
              >
                <option value="conversation_tree">Conversation Tree</option>
                <option value="branch">Branch</option>
                <option value="workspace">Workspace</option>
                <option value="user_global">User Global</option>
              </select>
              <textarea
                aria-label="Runtime v2 记忆内容"
                onChange={(event) => setMemoryDraft(event.target.value)}
                placeholder="写入当前 lane 的记忆…"
                rows={2}
                value={memoryDraft}
              />
              <button
                disabled={
                  !selectedLaneId || !memoryDraft.trim() || busy !== null
                }
                onClick={() => void createMemory()}
                type="button"
              >
                写入记忆
              </button>
            </div>
            {memoryPromotions.length > 0 ? (
              <div className="runtime-v2-memory-promotions">
                <strong>待确认提升</strong>
                {memoryPromotions.map((promotion) => (
                  <div key={promotion.id}>
                    <span>{promotion.targetScope}</span>
                    {promotion.conflictMemoryId ? (
                      <span className="runtime-v2-conflict">已有同内容记忆</span>
                    ) : null}
                    <p>
                      {memories.find(
                        (memory) => memory.id === promotion.memoryId,
                      )?.content ?? promotion.memoryId}
                    </p>
                    <div className="runtime-v2-actions">
                      <button
                        disabled={busy !== null}
                        onClick={() =>
                          void resolveMemoryPromotion(promotion.id, "accept")
                        }
                        type="button"
                      >
                        确认
                      </button>
                      <button
                        disabled={busy !== null}
                        onClick={() =>
                          void resolveMemoryPromotion(promotion.id, "reject")
                        }
                        type="button"
                      >
                        拒绝
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {snapshot.interruptedRuns.map((report) => (
            <div className="runtime-v2-recovery" key={report.runId}>
              <strong>中断 Run：{report.status}</strong>
              <p>
                {report.classification} · {report.action}
              </p>
              <ul>
                {report.findings.map((finding, index) => (
                  <li key={`${finding.reason}-${index}`}>{finding.message}</li>
                ))}
              </ul>
              <div className="runtime-v2-actions">
                <button
                  disabled={busy !== null}
                  onClick={() => void resolveRecovery(report.runId, "retry")}
                  type="button"
                >
                  检查后重试
                </button>
                <button
                  disabled={busy !== null}
                  onClick={() =>
                    void resolveRecovery(report.runId, "mark_failed")
                  }
                  type="button"
                >
                  标记失败
                </button>
              </div>
            </div>
          ))}

          {activeRun ? (
            <div className="runtime-v2-card">
              <strong>Run 状态</strong>
              <p>{activeRun.partialContent || "等待输出…"}</p>
              <div className="runtime-v2-turns">
                {activeRun.modelTurns.map((turn) => (
                  <div key={turn.id}>
                    <span>
                      ModelTurn {turn.index} · {turn.status}
                    </span>
                    <span>
                      tokens {turn.inputTokens ?? 0}/{turn.outputTokens ?? 0}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {runVariants.length > 0 ? (
            <div className="runtime-v2-variants">
              <div className="runtime-v2-lane-toolbar">
                <strong>RunVariants</strong>
                <button
                  disabled={busy !== null || isActiveRun}
                  onClick={() => void regenerateActiveVariant()}
                  type="button"
                >
                  重新生成
                </button>
              </div>
              {runVariants.map((variant) => (
                <div
                  className={
                    variant.isActiveVariant
                      ? "runtime-v2-lane is-selected"
                      : "runtime-v2-lane"
                  }
                  key={variant.runId}
                >
                  <button
                    disabled={
                      busy !== null ||
                      isActiveRun ||
                      variant.isActiveVariant ||
                      variant.status !== "completed"
                    }
                    onClick={() => void selectRunVariant(variant.runId)}
                    type="button"
                  >
                    <span>{variant.status}</span>
                    <strong>{variant.runId}</strong>
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          {snapshot.pendingApprovals.map((approval) => (
            <div className="runtime-v2-approval" key={approval.id}>
              <strong>{approval.summary}</strong>
              <p>{approval.reason}</p>
              <div className="runtime-v2-actions">
                <button
                  disabled={busy !== null}
                  onClick={() => void resolveApproval(approval.id, "approve")}
                  type="button"
                >
                  允许
                </button>
                <button
                  disabled={busy !== null}
                  onClick={() => void resolveApproval(approval.id, "deny")}
                  type="button"
                >
                  拒绝
                </button>
              </div>
            </div>
          ))}

          <div className="runtime-v2-entries">
            {snapshot.entries.map((entry) => (
              <div key={entry.id}>
                <span>{entryText[entry.type] ?? entry.type}</span>
                <p>
                  {entry.data.content ??
                    entry.data.toolName ??
                    "中间状态记录"}
                </p>
              </div>
            ))}
          </div>

          <div className="runtime-v2-events">
            {events.slice(-30).map((event) => (
              <div key={event.eventId}>
                <span>#{event.eventSeq}</span>
                <strong>{event.type}</strong>
              </div>
            ))}
          </div>
        </>
      ) : (
        <p className="runtime-v2-empty">
          {conversationId ? "正在加载 Runtime v2 状态…" : "暂无会话。"}
        </p>
      )}

      <div className="runtime-v2-composer">
        <textarea
          aria-label="Runtime v2 消息"
          disabled={!conversationId || busy !== null}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={isActiveRun ? "向当前 Run 插话…" : "发送 Runtime v2 消息…"}
          rows={2}
          value={draft}
        />
        <div>
          <button
            disabled={!conversationId || !draft.trim() || busy !== null}
            onClick={() => void submit()}
            type="button"
          >
            {isActiveRun ? "插话" : "发送"}
          </button>
          {isActiveRun ? (
            <button
              disabled={busy !== null}
              onClick={() => void cancelRun()}
              type="button"
            >
              停止
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}
