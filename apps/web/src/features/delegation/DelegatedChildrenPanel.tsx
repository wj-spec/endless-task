import { useCallback, useEffect, useRef, useState } from "react";
import { ApiClientError, chatApi } from "../chat/api";
import type { DelegatedChildRun } from "../chat/apiTypes";
import { EmptyState } from "../ui/EmptyState";

type DelegatedChildrenPanelProps = {
  conversationId: string | null;
};

/** 服务端固定的六个状态取值：一个都不能漏（`queued` 是还在并发闸门外的行）。 */
const CHILD_STATUS_LABELS: Record<DelegatedChildRun["status"], string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

/** 这些错误码的中文 safeMessage 可以直接给用户看（其余一律回落到固定文案）。 */
const VISIBLE_ERROR_CODES = new Set(["delegation_child_owner_unavailable"]);

/** 已用时文案：null 表示还没开始（排队中）。 */
const elapsedText = (seconds: number | null) => {
  if (seconds === null) return "尚未开始";
  if (seconds < 60) return `已用时 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const restSeconds = seconds % 60;
    return restSeconds
      ? `已用时 ${minutes} 分 ${restSeconds} 秒`
      : `已用时 ${minutes} 分`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes
    ? `已用时 ${hours} 小时 ${restMinutes} 分`
    : `已用时 ${hours} 小时`;
};

/**
 * 右栏「子任务」页签：本会话派过的子运行（最近创建的在前，最多 50 条）。
 *
 * 取数范式与 `NotificationsContent` 一致：首屏加载一次、动作后重新加载，**不轮询**。
 * 子运行状态只在打开页签与动作后刷新（不订阅 SSE）——页签是用户停留时才看的表面。
 *
 * 面板挂在 `AssistantPanel` 上、**不随会话换 key**，所以会话切换只是换 prop：
 * 状态必须跟着 `conversationId` 走——每次加载先清空上一个会话的数据并进入加载态，
 * 再用请求代次（`requestGeneration`）丢弃过期响应，避免"上一个会话的行还在 /
 * 慢响应最后落地 / 拿旧 childRunId 配新 conversationId 去取消"。
 */
export function DelegatedChildrenPanel({
  conversationId,
}: DelegatedChildrenPanelProps) {
  const [items, setItems] = useState<DelegatedChildRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  /** 每次加载自增；只有"当前代次"的响应与收尾才允许写状态。 */
  const requestGeneration = useRef(0);

  const load = useCallback(async () => {
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    // 先回到加载态：上一个会话（或上一次请求）的数据一律不能留在界面上。
    setLoading(true);
    setItems([]);
    setLoadError(null);
    setActionError(null);
    setBusyId(null);
    if (!conversationId) {
      // 没有选中会话就没有可看的子运行：空态而不是错误。
      setLoading(false);
      return;
    }
    try {
      const next = await chatApi.listDelegatedChildren(conversationId);
      if (requestGeneration.current !== generation) return; // 过期响应：整包丢弃
      setItems(next);
      setLoadError(null);
    } catch {
      if (requestGeneration.current !== generation) return;
      setLoadError("无法加载子任务，请重试。");
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load]);

  const cancel = async (item: DelegatedChildRun) => {
    if (!conversationId) return;
    const generation = requestGeneration.current;
    setBusyId(item.childRunId);
    setActionError(null);
    try {
      await chatApi.cancelDelegatedChild(conversationId, item.childRunId);
      // 会话已经切走时不能再用"旧会话的 load"去覆盖新会话的列表（它还会把
      // 新会话正在飞的请求挤成过期）。新会话自己会加载，这里直接放手。
      if (requestGeneration.current !== generation) return;
      await load();
    } catch (error) {
      // 代次变了（切了会话或有更新的加载）就不再往当前界面上写这次失败。
      if (requestGeneration.current !== generation) return;
      // 失败不静默，也不谎报"已取消"：拥有者协调器不在本进程内是可重试的 409。
      setActionError(
        error instanceof ApiClientError && VISIBLE_ERROR_CODES.has(error.code)
          ? error.message
          : "取消失败，请重试。",
      );
    } finally {
      // 加载期间已经重置过 busyId；只有代次未变（失败且未重载）时才由这里收尾。
      if (requestGeneration.current === generation) setBusyId(null);
    }
  };

  return (
    <div className="panel-content">
      {items.length > 0 ? (
        <div className="panel-toolbar">
          {/* total 是返回条数（最多 50），不是会话的子运行总数：只能说"最近 N 条"。 */}
          <span>最近 {items.length} 条</span>
        </div>
      ) : null}
      {loading ? (
        <div aria-hidden="true" className="skeleton-panel">
          <span className="skeleton-line" />
          <span className="skeleton-line is-short" />
          <span className="skeleton-line" />
        </div>
      ) : null}
      {!loading && loadError ? (
        <EmptyState
          action={
            <button onClick={() => void load()} type="button">
              重试
            </button>
          }
          desc={loadError}
          title="没加载出来"
        />
      ) : null}
      {!loading && !loadError && items.length === 0 ? (
        <EmptyState
          desc="主 Agent 派生子代理后，这里会列出它们的进度和结果。"
          title="还没有子任务"
        />
      ) : null}
      {!loading && !loadError && actionError ? (
        <div className="proposal-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {!loading && !loadError
        ? items.map((item) => (
            <div className="memory-item" key={item.childRunId}>
              <div className="memory-content">
                <strong>{item.task}</strong>
              </div>
              <div className="memory-meta">
                <span className="proposal-kind">
                  {CHILD_STATUS_LABELS[item.status] ?? "未知"}
                </span>
                <span>{elapsedText(item.elapsedSeconds)}</span>
              </div>
              {item.summary ? (
                <p className="proposal-reason">{item.summary}</p>
              ) : null}
              {item.status === "running" || item.status === "queued" ? (
                <div className="memory-actions">
                  <button
                    disabled={busyId === item.childRunId}
                    onClick={() => void cancel(item)}
                    type="button"
                  >
                    取消
                  </button>
                </div>
              ) : null}
            </div>
          ))
        : null}
    </div>
  );
}
