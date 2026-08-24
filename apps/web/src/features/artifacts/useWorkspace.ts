import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { WorkspaceSnapshot } from "../chat/apiTypes";

export function useWorkspace(
  conversationId: string | null,
  pendingArtifactProposalCount: number,
) {
  const [workspace, setWorkspace] = useState<WorkspaceSnapshot | null>(null);

  const refresh = useCallback(async () => {
    if (!conversationId) return;
    try {
      const snapshot = await chatApi.getWorkspace(conversationId);
      setWorkspace(snapshot);
    } catch {
      // 工作区是增强信息，拉取失败时静默降级，不打断聊天主流程。
    }
  }, [conversationId]);

  useEffect(() => {
    setWorkspace(null);
    void refresh();
  }, [refresh, pendingArtifactProposalCount]);

  return { workspace, refresh };
}
