import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "../chat/api";
import type { PendingProposal, TaskNotification } from "../chat/apiTypes";

export type AssistantHub = {
  unread: number;
  proposals: PendingProposal[];
  total: number;
  refresh: () => void;
};

export function useAssistantHub(
  onFreshNotifications: (items: TaskNotification[]) => void,
): AssistantHub {
  const [unread, setUnread] = useState(0);
  const [proposals, setProposals] = useState<PendingProposal[]>([]);
  const onFreshRef = useRef(onFreshNotifications);
  onFreshRef.current = onFreshNotifications;
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let cancelled = false;
    const known = new Set<string>();
    let seeded = false;
    const poll = async () => {
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
    pollRef.current = () => void poll();
    void poll();
    const timer = globalThis.setInterval(() => void poll(), 10000);
    const wake = () => void poll();
    globalThis.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      cancelled = true;
      globalThis.clearInterval(timer);
      globalThis.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);

  return { unread, proposals, total: unread + proposals.length, refresh };
}
