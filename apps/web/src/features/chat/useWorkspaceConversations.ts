import { useCallback, useMemo } from "react";
import { chatApi } from "./api";
import { readableError } from "./apiErrorText";
import { WORKSPACE_STORAGE_KEY } from "./chatApplicationSupport";
import type {
  CapabilitySnapshot,
  Conversation,
  ConversationStatus,
  HealthSnapshot,
  ProviderProfile,
  Workspace,
} from "./apiTypes";

/**
 * 会话列表与工作区：拉取/切换/新建/删除工作区，以及按状态与工作区加载会话列表。
 *
 * 从 `useChatApplication.ts`（1 454 行）抽出。这一簇是"列表数据源"——父层几乎只读它
 * 产出的数据与动作，它自己不碰运行时、车道、侧栏。
 *
 * **两个 ref 留在父层**（作为参数传入）：`activeConversationIdRef` / `conversationsRef`
 * —— 父层还有别的 effect（hashchange 路由、卸载清理）要用它们，搬进来又要再返回。
 * **两个派生值**（`currentWorkspace` / `workspaceCanCreate`）随本簇搬入并返回，因为
 * `workspaceCanCreate` 是 `loadConversationList` 的判据，两者必须一起走。
 *
 * 行为零改动：11 个成员逐行原样搬运，只把闭包里的量改成参数解构。
 */
export type WorkspaceConversationsParams = {
  onSelectWorkspace?: () => void;
  conversationListVersion: React.MutableRefObject<number>;
  activeConversationIdRef: React.MutableRefObject<string | null>;
  conversationsRef: React.MutableRefObject<Conversation[]>;
  conversations: Conversation[];
  workspaces: Workspace[];
  homePath: string;
  workspaceId: string | null;
  search: string;
  statusFilter: ConversationStatus;
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  setConversationsRevision: React.Dispatch<React.SetStateAction<number>>;
  setWorkspaces: React.Dispatch<React.SetStateAction<Workspace[]>>;
  setHomePath: React.Dispatch<React.SetStateAction<string>>;
  setWorkspaceId: React.Dispatch<React.SetStateAction<string | null>>;
  setActiveConversationId: React.Dispatch<React.SetStateAction<string | null>>;
  setLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setError: React.Dispatch<React.SetStateAction<string | null>>;
  setHealth: React.Dispatch<React.SetStateAction<HealthSnapshot | null>>;
  setCapabilities: React.Dispatch<
    React.SetStateAction<CapabilitySnapshot | null>
  >;
  setProviders: React.Dispatch<React.SetStateAction<ProviderProfile[]>>;
  openConversation: (
    conversationId: string,
    options?: { focusTurnId?: string | null },
  ) => Promise<void>;
};

export function useWorkspaceConversations({
  onSelectWorkspace,
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
}: WorkspaceConversationsParams) {

  const bumpConversationsRevision = useCallback(
    () => setConversationsRevision((value) => value + 1),
    [],
  );

  const currentWorkspace = useMemo(
    () => workspaces.find((item) => item.id === workspaceId) ?? null,
    [workspaces, workspaceId],
  );

  const workspaceCanCreate = Boolean(currentWorkspace?.rootPath);

  const loadConversationList = useCallback(
    async (status: ConversationStatus, preferredId?: string) => {
      // 请求版本守卫：列表加载可能因 workspaceId/状态切换被并发触发，
      // 旧响应不得覆盖新状态（尤其刷新恢复时）。
      const requestVersion = conversationListVersion.current + 1;
      conversationListVersion.current = requestVersion;
      const isCurrent = () => conversationListVersion.current === requestVersion;
      // 请求发出时的活动会话：响应回来时若用户/代码已经切到别的会话，
      // 这次响应就只能更新列表，**不能**再按旧意图打开会话——否则一个迟到的
      // 响应会把"刚新建/刚点开"的会话顶掉（表现为 + 新建后跳回上一段对话）。
      const activeAtRequest = activeConversationIdRef.current;
      setLoading(true);
      setError(null);
      try {
        let items = await chatApi.listConversations(status, undefined, workspaceId);
        if (!isCurrent()) return;
        if (status === "active" && items.length === 0 && workspaceCanCreate) {
          const created = await chatApi.createConversation(workspaceId!);
          if (!isCurrent()) return;
          items = [created];
        }
        setConversations(items);
        // 选择打开哪一个：显式 preferred 优先 → 否则保留**当前仍在列表里的会话**
        // → 再退回第一项。此前是无条件打开 items[0]，于是列表刷新（页面初始加载、
        // 工作区刷新等）会把用户"刚点开/刚新建"的会话顶掉——表现为点了「+ 新建会话」
        // 之后主对话又跳回上一段对话。
        const current = activeConversationIdRef.current;
        const superseded = current !== activeAtRequest;
        const preferred = superseded
          ? null
          : (items.find((item) => item.id === preferredId)?.id ?? null);
        // 同工作区的刷新即使没包含刚新建的会话（请求早于创建发出），也要保留它。
        const known = conversationsRef.current.find((item) => item.id === current);
        const currentStillValid =
          Boolean(current) &&
          (items.some((item) => item.id === current) ||
            (known?.workspaceId ?? null) === (workspaceId ?? null));
        const target =
          preferred ??
          (currentStillValid ? current : null) ??
          (superseded ? null : (items[0]?.id ?? null));
        if (isCurrent()) {
          if (target && target !== current) await openConversation(target);
          else if (!target) {
            setActiveConversationId(null);
            setLoading(false);
          } else {
            setLoading(false);
          }
        }
      } catch (loadError) {
        if (isCurrent()) {
          setError(readableError(loadError));
          setLoading(false);
        }
      }
    },
    [openConversation, workspaceId, workspaceCanCreate],
  );

  const refreshCapabilities = useCallback(async () => {
    try {
      setCapabilities(await chatApi.capabilities());
    } catch {
      setCapabilities(null);
    }
  }, []);

  const refreshProviders = useCallback(async () => {
    try {
      setProviders(await chatApi.listProviders());
    } catch {
      setProviders([]);
    }
    try {
      setHealth(await chatApi.health());
    } catch {
      setHealth(null);
    }
    await refreshCapabilities();
  }, [refreshCapabilities]);

  const visibleConversations = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return conversations;
    return conversations.filter((item) => item.title.toLocaleLowerCase().includes(query));
  }, [conversations, search]);

  const selectWorkspace = (nextWorkspaceId: string | null) => {
    onSelectWorkspace?.();
    setWorkspaceId(nextWorkspaceId);
    try {
      globalThis.localStorage?.setItem(
        WORKSPACE_STORAGE_KEY,
        nextWorkspaceId ?? "general",
      );
    } catch {
      // 存储不可用时仅内存态，忽略。
    }
  };

  const createWorkspace = async (name: string, rootPath?: string | null) => {
    const workspace = await chatApi.createWorkspace(name, rootPath);
    setWorkspaces((current) => [...current, workspace]);
    selectWorkspace(workspace.id);
    return workspace;
  };

  const refreshWorkspaces = async () => {
    try {
      const { items, homePath } = await chatApi.listWorkspaces();
      setWorkspaces(items);
      setHomePath(homePath);
    } catch {
      // 列表刷新失败保持现有状态，忽略。
    }
  };

  const deleteWorkspace = async (workspaceIdToDelete: string) => {
    await chatApi.deleteWorkspace(workspaceIdToDelete);
    setWorkspaces((current) =>
      current.filter((item) => item.id !== workspaceIdToDelete),
    );
    if (workspaceId === workspaceIdToDelete) {
      selectWorkspace(null);
    }
  };

  return {
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
  };
}
