import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { TerminalSnapshot } from "../chat/apiTypes";

export type TerminalSessionsController = {
  sessions: TerminalSnapshot[];
  activeId: string | null;
  busy: boolean;
  error: string | null;
  refresh: () => void;
  create: () => void;
  select: (sessionId: string) => void;
  close: (sessionId: string) => void;
};

/** S4：按工作区管理终端会话（列表 / 新建 / 关闭）。 */
export function useTerminalSessions(
  workspaceId: string,
): TerminalSessionsController {
  const [sessions, setSessions] = useState<TerminalSnapshot[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!workspaceId) {
      setSessions([]);
      return;
    }
    void chatApi
      .listWorkspaceTerminals(workspaceId)
      .then((items) => {
        setSessions(items);
        setActiveId((current) => {
          if (current && items.some((item) => item.sessionId === current)) {
            return current;
          }
          const alive = items.find((item) => item.status.kind === "running");
          return alive?.sessionId ?? items[0]?.sessionId ?? null;
        });
      })
      .catch((cause: unknown) => {
        setError(readableError(cause) || "无法读取终端会话。");
      });
  }, [workspaceId]);

  useEffect(() => {
    setError(null);
    setSessions([]);
    setActiveId(null);
    refresh();
  }, [refresh]);

  const create = useCallback(() => {
    if (!workspaceId) return;
    setBusy(true);
    setError(null);
    void chatApi
      .createWorkspaceTerminal(workspaceId, { name: "" })
      .then((terminal) => {
        setSessions((previous) => [...previous, terminal]);
        setActiveId(terminal.sessionId);
      })
      .catch((cause: unknown) => {
        setError(readableError(cause) || "创建终端失败。");
      })
      .finally(() => setBusy(false));
  }, [workspaceId]);

  const close = useCallback(
    (sessionId: string) => {
      if (!workspaceId) return;
      setBusy(true);
      setError(null);
      void chatApi
        .closeWorkspaceTerminal(workspaceId, sessionId)
        .then(() => {
          setSessions((previous) =>
            previous.filter((item) => item.sessionId !== sessionId),
          );
          setActiveId((current) => (current === sessionId ? null : current));
        })
        .catch((cause: unknown) => {
          setError(readableError(cause) || "关闭终端失败。");
        })
        .finally(() => setBusy(false));
    },
    [workspaceId],
  );

  return {
    sessions,
    activeId,
    busy,
    error,
    refresh,
    create,
    select: setActiveId,
    close,
  };
}
