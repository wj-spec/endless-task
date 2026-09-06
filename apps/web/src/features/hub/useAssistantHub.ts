import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi, streamHubV2Events } from "../chat/api";
import type { PendingProposal, TaskNotification } from "../chat/apiTypes";

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
        seeded = true;
        return;
      }
      if (fresh.length > 0) onFreshRef.current(fresh);
    };
    pollRef.current = () => void pollOnce();
    void pollOnce();

    // SSE 推送为主；仅当推送失败/断线时启用 10s 轮询兜底。
    let controller: AbortController | null = null;
    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;
    let disconnected = false;

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
                const targets = hubEventTargets(event.type);
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
