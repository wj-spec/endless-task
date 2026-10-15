import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi, streamHubV2Events } from "../chat/api";
import type { PendingProposal, TaskNotification } from "../chat/apiTypes";
import { publishHubEvent } from "./hubEvents";

export type AssistantHub = {
  unread: number;
  proposals: PendingProposal[];
  total: number;
  refresh: () => void;
};

/** P1-2 推送相关事件：收到即刷新对应数据源；无关事件忽略。 */
export function hubEventTargets(
  type: string,
): { notifications: boolean; proposals: boolean } {
  if (
    type === "notification.created" ||
    type === "notification.read" ||
    type === "notification.read_all"
  ) {
    return { notifications: true, proposals: false };
  }
  if (type === "proposal.pending" || type === "proposal.resolved") {
    return { notifications: false, proposals: true };
  }
  return { notifications: false, proposals: false };
}

/**
 * 切片 10 A2（item 6）：广播 + 分类的**单一入口**。
 *
 * 两条拿到数据的路径（SSE `onEvent` 与断线兜底 `pollOnce`）共用它，顺序被钉死一次：
 * **先广播、后按 `hubEventTargets` 过滤**——订阅者自己按类型决定要不要重拉，无关类型
 * 也照样广播（T4 item 3 的既有约定）。广播只带**事件类型**，不搬载荷。
 */
const broadcastHubEvent = (
  type: string,
): { notifications: boolean; proposals: boolean } => {
  publishHubEvent(type);
  return hubEventTargets(type);
};

const FALLBACK_POLL_MS = 10_000;
const RECONNECT_DELAY_MS = 1_500;

export function useAssistantHub(
  onFreshNotifications: (items: TaskNotification[]) => void,
): AssistantHub {
  const [unread, setUnread] = useState(0);
  const [proposals, setProposals] = useState<PendingProposal[]>([]);
  const onFreshRef = useRef(onFreshNotifications);
  onFreshRef.current = onFreshNotifications;
  const pollRef = useRef<() => void>(() => {});
  const cursorRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const known = new Set<string>();
    let seeded = false;
    // SSE 是否已断开（=现在是 10s 轮询兜底）。`pollOnce` 的闭包要读它，故先声明。
    let disconnected = false;

    const pollOnce = async () => {
      const [notifications, pending] = await Promise.all([
        chatApi.listNotifications(true).catch(() => [] as TaskNotification[]),
        chatApi.listPendingProposals().catch(() => [] as PendingProposal[]),
      ]);
      if (cancelled) return;
      setUnread(notifications.length);
      setProposals(pending);
      const fresh = notifications.filter((item) => !known.has(item.id));
      notifications.forEach((item) => known.add(item.id));
      if (!seeded) {
        // 首次只是把服务端已有的列表灌进基线（面板自己会 load），
        // **不算**"新到的通知"——否则每次挂载都会凭空广播一轮。
        seeded = true;
        return;
      }
      if (fresh.length > 0) {
        // 切片 10 A2（item 6）：兜底轮询拿到的**新通知**也要广播。
        //
        // 此前 `pollOnce` 只更新自己的 unread/proposals，从不 `publishHubEvent`：
        // hub 断线期间（SSE 挂了、只剩 10s 轮询）后端到点触发的任务/提醒终态
        // 通知因此推不到「已安排」面板——面板只能靠自己的 focus/visibility 补拉。
        // 广播的就是后端为终态通知发的那个类型（见 `ScheduledTasks` 的
        // `HUB_REFRESH_EVENT_TYPES`），订阅者自己决定拉什么数据。
        //
        // 切片 10 A3（Fix 1）：**只在 SSE 断线时**广播。后端 `notification_service`
        // 先落库、再发 hub 事件，所以 SSE 在线时 `onEvent` 已经广播过一次；紧随
        // 其后的 `pollOnce` 必然把同一条捞成 fresh，若在这里再广播一次，唯一订阅者
        // （`ScheduledTasks`）就会为同一条通知静默重拉两遍。在线路径以 SSE 那次为
        // 准，这里只在断线兜底路径补广播。
        //
        // 交给调用方的 `onFreshNotifications`（`App.tsx` 的 toast）**不**受此门控：
        // SSE `onEvent` 从不调它，在线时这条路径是通知 toast 的唯一来源。
        if (disconnected) {
          broadcastHubEvent("notification.created");
        }
        onFreshRef.current(fresh);
      }
    };
    pollRef.current = () => void pollOnce();
    void pollOnce();

    // SSE 推送为主；仅当推送失败/断线时启用 10s 轮询兜底。
    let controller: AbortController | null = null;
    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;

    const stopFallback = () => {
      if (fallbackTimer !== null) {
        globalThis.clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };

    const startFallback = () => {
      if (fallbackTimer !== null) return;
      fallbackTimer = globalThis.setInterval(() => void pollOnce(), FALLBACK_POLL_MS);
    };

    const openStream = async () => {
      if (cancelled) return;
      controller = new AbortController();
      const signal = controller.signal;
      let reconnecting = false;
      try {
        while (!signal.aborted && !cancelled) {
          try {
            await streamHubV2Events({
              afterSequence: cursorRef.current,
              signal,
              onEvent: (event) => {
                if (event.eventSeq > cursorRef.current) {
                  cursorRef.current = event.eventSeq;
                }
                // 切片 10 T4（item 3）：广播给页面内其它订阅者（如「已安排」面板），
                // 让它们在**同一条** hub 流上实时化，不再各开一条 SSE。
                // 广播在 `hubEventTargets` 过滤之前：订阅者自己按类型决定要不要重拉。
                const targets = broadcastHubEvent(event.type);
                if (!targets.notifications && !targets.proposals) return;
                if (disconnected) {
                  disconnected = false;
                  stopFallback();
                }
                void pollOnce();
              },
            });
            // 正常结束（服务端 idle 断连）→ 立即重连。
          } catch (error) {
            if (cancelled || signal.aborted) return;
            reconnecting = true;
          }
          if (cancelled || signal.aborted) return;
          if (!disconnected) {
            disconnected = true;
            startFallback();
          }
          await new Promise((resolve) => globalThis.setTimeout(resolve, RECONNECT_DELAY_MS));
        }
      } finally {
        if (controller?.signal === signal) controller = null;
      }
    };
    void openStream();

    const wake = () => {
      // 兜底：焦点/可见性恢复时补拉一次（与推送并存）。
      if (disconnected) void pollOnce();
    };
    globalThis.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);

    return () => {
      cancelled = true;
      controller?.abort();
      stopFallback();
      globalThis.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);

  return { unread, proposals, total: unread + proposals.length, refresh };
}
