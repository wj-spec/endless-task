import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "./api";
import type { Conversation, ConversationStatus, Workspace } from "./apiTypes";

type WorkspaceNavigationData = {
  byWorkspace: Record<string, Conversation[]>;
  general: Conversation[];
};

type WorkspaceNavigationState = WorkspaceNavigationData & {
  loading: boolean;
  error: string | null;
  refresh: () => void;
};

/**
 * Rail 专用的导航数据源。主聊天流程继续使用 `useChatApplication.conversations`；
 * 此处仅为 Rail 并发拉取每个真实工作区 + General（未归属）的会话，并在工作区列表、
 * 状态 tab、query、以及会话增删改/归档后可靠刷新。
 *
 * 本地单用户规模下，N 个工作区的 N+1 次并发可接受。
 */
export function useWorkspaceNavigation(
  workspaces: Workspace[],
  status: ConversationStatus,
  query: string,
): WorkspaceNavigationState {
  const [data, setData] = useState<WorkspaceNavigationData>({
    byWorkspace: {},
    general: [],
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  const refresh = useCallback(() => {
    const version = requestVersion.current + 1;
    requestVersion.current = version;
    setLoading(true);
    setError(null);

    const trimmedQuery = query.trim();
    const workspaceRequests = workspaces.map((workspace) =>
      chatApi.listConversations(status, trimmedQuery, workspace.id),
    );
    const generalRequest = chatApi.listConversations(status, trimmedQuery, null);

    void Promise.all([...workspaceRequests, generalRequest])
      .then((results) => {
        if (requestVersion.current !== version) return;
        const byWorkspace: Record<string, Conversation[]> = {};
        workspaces.forEach((workspace, index) => {
          byWorkspace[workspace.id] = results[index];
        });
        setData({
          byWorkspace,
          general: results[workspaces.length],
        });
        setLoading(false);
      })
      .catch(() => {
        if (requestVersion.current !== version) return;
        setError("无法加载工作区会话。");
        setLoading(false);
      });
  }, [workspaces, status, query]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return {
    ...data,
    loading,
    error,
    refresh,
  };
}