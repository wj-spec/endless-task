import { useState } from "react";
import type {
  Conversation,
  ConversationStatus,
  Workspace,
} from "./apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { EmptyState } from "../ui/EmptyState";
import { RowMenu } from "../ui/RowMenu";
import { SidebarToggleIcon } from "../ui/SidebarToggleIcon";

type SessionRailProps = {
  activeConversationId: string | null;
  collapsed: boolean;
  conversations: Conversation[];
  open: boolean;
  search: string;
  statusFilter: ConversationStatus;
  workspaceId: string | null;
  workspaces: Workspace[];
  pendingTotal: number;
  onCreateWorkspace: (name: string) => Promise<unknown>;
  onSelectWorkspace: (workspaceId: string | null) => void;
  onOpenAssistant: (tab: "notifications" | "memory") => void;
  onOpenSettings: () => void;
  onChangeConversationStatus: (
    conversationId: string,
    status: ConversationStatus,
  ) => void;
  onClose: () => void;
  onDeleteConversation: (conversationId: string) => void;
  onNewConversation: () => void;
  onRenameConversation: (conversationId: string, title: string) => void;
  onSearchChange: (value: string) => void;
  onSelectConversation: (conversationId: string) => void;
  onStatusFilterChange: (status: ConversationStatus) => void;
};

const formatRelativeTime = (value: string) => {
  const timestamp = new Date(value).getTime();
  const elapsed = Date.now() - timestamp;
  const minutes = Math.max(0, Math.floor(elapsed / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(
    new Date(value),
  );
};

export function SessionRail({
  activeConversationId,
  collapsed,
  conversations,
  open,
  search,
  statusFilter,
  workspaceId,
  workspaces,
  pendingTotal,
  onCreateWorkspace,
  onSelectWorkspace,
  onOpenAssistant,
  onOpenSettings,
  onChangeConversationStatus,
  onClose,
  onDeleteConversation,
  onNewConversation,
  onRenameConversation,
  onSearchChange,
  onSelectConversation,
  onStatusFilterChange,
}: SessionRailProps) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);

  const handleWorkspaceChange = async (value: string) => {
    if (value === "__create__") {
      if (creatingWorkspace) return;
      const name = globalThis.window?.prompt("新工作区名称：");
      const trimmed = (name ?? "").trim();
      if (!trimmed) return;
      setCreatingWorkspace(true);
      try {
        await onCreateWorkspace(trimmed);
      } finally {
        setCreatingWorkspace(false);
      }
      return;
    }
    onSelectWorkspace(value === "general" ? null : value);
  };
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const primaryConversations = conversations.filter(
    (item) => item.kind !== "ephemeral",
  );

  const startRename = (conversation: Conversation) => {
    setRenamingId(conversation.id);
    setRenameDraft(conversation.title);
  };

  const commitRename = () => {
    if (!renamingId) return;
    const title = renameDraft.trim();
    if (title) onRenameConversation(renamingId, title);
    setRenamingId(null);
  };
  const railExpanded = open || !collapsed;
  const railToggleLabel = railExpanded ? "收起侧栏" : "展开侧栏";

  return (
    <aside
      aria-label="会话导航"
      className={`session-rail${open ? " is-open" : ""}${
        collapsed ? " is-collapsed" : ""
      }`}
    >
      <header className="rail-header">
        <div className="wordmark" aria-label="Endless Task">
          <span className="wordmark-symbol">∞</span>
          <span>Endless</span>
        </div>
        <button
          aria-label={railToggleLabel}
          className="icon-button rail-toggle"
          onClick={onClose}
          title={railToggleLabel}
          type="button"
        >
          <SidebarToggleIcon expanded={railExpanded} />
        </button>
      </header>

      <div className="workspace-switcher">
        <label className="sr-only" htmlFor="workspace-switcher-select">
          当前工作区
        </label>
        <select
          disabled={creatingWorkspace}
          id="workspace-switcher-select"
          onChange={(event) => void handleWorkspaceChange(event.target.value)}
          value={workspaceId ?? "general"}
        >
          <option value="general">通用</option>
          {workspaces.map((workspace) => (
            <option key={workspace.id} value={workspace.id}>
              {workspace.name}
            </option>
          ))}
          <option value="__create__">＋ 新建工作区…</option>
        </select>
      </div>

      <button
        aria-label="新对话"
        className="new-chat-button"
        onClick={onNewConversation}
        title={collapsed ? "新对话" : undefined}
        type="button"
      >
        <span aria-hidden="true" className="new-chat-icon">＋</span>
        <span className="new-chat-label">新对话</span>
      </button>

      <label className="conversation-search">
        <span className="sr-only">搜索对话</span>
        <span aria-hidden="true">⌕</span>
        <input
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="搜索对话"
          type="search"
          value={search}
        />
      </label>

      <div className="rail-filter" role="tablist" aria-label="会话状态">
        <button
          aria-selected={statusFilter === "active"}
          className={statusFilter === "active" ? "is-active" : ""}
          onClick={() => onStatusFilterChange("active")}
          role="tab"
          type="button"
        >
          最近
        </button>
        <button
          aria-selected={statusFilter === "archived"}
          className={statusFilter === "archived" ? "is-active" : ""}
          onClick={() => onStatusFilterChange("archived")}
          role="tab"
          type="button"
        >
          已归档
        </button>
      </div>

      <nav className="session-list" aria-label="会话列表">
        {primaryConversations.length === 0 ? (
          <EmptyState
            className="rail-empty"
            desc={search ? "换个关键词试试。" : "点「新对话」开始，助手会记住你们聊过什么。"}
            title={search ? "没有匹配的对话" : "这里还没有对话"}
          />
        ) : null}
        {primaryConversations.map((conversation) => {
          const isActive = conversation.id === activeConversationId;
          const archived = conversation.status === "archived";
          return (
            <div
              className={isActive ? "session-item is-active" : "session-item"}
              key={conversation.id}
            >
              {renamingId === conversation.id ? (
                <div className="session-rename">
                  <input
                    autoFocus
                    onBlur={commitRename}
                    onChange={(event) => setRenameDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") commitRename();
                      if (event.key === "Escape") setRenamingId(null);
                    }}
                    value={renameDraft}
                  />
                </div>
              ) : (
                <button
                  aria-current={isActive ? "page" : undefined}
                  className="session-item-main"
                  onClick={() => onSelectConversation(conversation.id)}
                  type="button"
                >
                  <span>{conversation.title}</span>
                  <time dateTime={conversation.updatedAt}>
                    {formatRelativeTime(conversation.updatedAt)}
                  </time>
                </button>
              )}
              <RowMenu
                className="session-row-menu"
                items={[
                  { label: "重命名", onSelect: () => startRename(conversation) },
                  {
                    label: archived ? "取消归档" : "归档",
                    onSelect: () =>
                      onChangeConversationStatus(
                        conversation.id,
                        archived ? "active" : "archived",
                      ),
                  },
                  {
                    danger: true,
                    label: "删除",
                    onSelect: () => setDeleteTarget(conversation),
                  },
                ]}
                triggerAriaLabel={`管理对话：${conversation.title}`}
                triggerClassName="session-menu-button"
              />
            </div>
          );
        })}
      </nav>

      <nav aria-label="应用入口" className="rail-footer">
        <button onClick={() => onOpenAssistant("notifications")} type="button">
          <span>任务与通知</span>
          {pendingTotal > 0 ? <strong>{pendingTotal}</strong> : null}
        </button>
        <button onClick={() => onOpenAssistant("memory")} type="button">
          知识与记忆
        </button>
        <button onClick={onOpenSettings} type="button">
          设置
        </button>
      </nav>

      {deleteTarget ? (
        <ConfirmDialog
          body="删除后无法恢复；该对话相关的安排与提醒会一并取消。"
          confirmLabel="删除"
          onClose={() => setDeleteTarget(null)}
          onConfirm={() => onDeleteConversation(deleteTarget.id)}
          title="删除这段对话？"
        />
      ) : null}
    </aside>
  );
}
