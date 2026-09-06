import type { Conversation, ConversationStatus, Workspace } from "./apiTypes";

/** 工作区组下的默认可见会话数；更多通过“展开显示”暴露。 */
export const WORKSPACE_VISIBLE_SESSIONS = 4;
/** 最近（未归属）组的默认可见会话数。 */
export const RECENT_VISIBLE_SESSIONS = 6;

/** 会话行共享动作，避免在多个导航组件间散落同名单一回调。 */
export type SessionActions = {
  onSelectConversation: (id: string) => void;
  onRenameStart: (id: string) => void;
  onRenameDraftChange: (value: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
  onChangeStatus: (id: string, status: ConversationStatus) => void;
  onDelete: (id: string) => void;
  /** 未绑定目录的工作区：请求打开绑定入口以承载会话。 */
  onBindWorkspace?: (workspaceId: string) => void;
  /** 删除工作区（有会话的工作区后端会拒绝；调用方负责确认/提示）。 */
  onDeleteWorkspace?: (workspaceId: string) => void;
  /** 在指定工作区里新建会话（调用方负责选中该工作区并创建）。 */
  onNewConversation?: (workspaceId: string) => void;
};

export type WorkspaceNavigationGroup = {
  id: string;
  name: string;
  rootPath: string | null;
  conversations: Conversation[];
  totalCount: number;
};

export type WorkspaceNavigationModel = {
  groups: WorkspaceNavigationGroup[];
  recent: Conversation[];
};

/** 会话标题可靠回退：空标题显示为“未命名对话”。 */
export function conversationTitle(conversation: Conversation): string {
  const title = conversation.title.trim();
  return title || "未命名对话";
}

const byUpdatedDesc = (a: Conversation, b: Conversation) =>
  new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();

/**
 * 纯派生：把每个工作区的会话与未归属（General）会话组织成导航 view model。
 * - 真实 workspaces 作为分组，各自按更新时间降序。
 * - General / 未归属会话只进入 `recent`，不作为伪工作区。
 * - 分组内会话与 recent 不重复（按 workspaceId 来源天然隔离）。
 */
export function buildWorkspaceNavigationModel({
  workspaces,
  byWorkspace,
  general,
}: {
  workspaces: Workspace[];
  byWorkspace: Record<string, Conversation[]>;
  general: Conversation[];
}): WorkspaceNavigationModel {
  const groups = workspaces.map((workspace) => {
    const conversations = [
      ...(byWorkspace[workspace.id] ?? []),
    ].sort(byUpdatedDesc);
    return {
      id: workspace.id,
      name: workspace.name,
      rootPath: workspace.rootPath,
      conversations,
      totalCount: conversations.length,
    };
  });
  const recent = [...general].sort(byUpdatedDesc);
  return { groups, recent };
}