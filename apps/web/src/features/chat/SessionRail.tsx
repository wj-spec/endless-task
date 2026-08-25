import { useState } from "react";
import type { Conversation, ConversationStatus } from "./apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { EmptyState } from "../ui/EmptyState";
import { RowMenu } from "../ui/RowMenu";

type SessionRailProps = {
  activeConversationId: string | null;
  conversations: Conversation[];
  open: boolean;
  search: string;
  statusFilter: ConversationStatus;
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
  conversations,
  open,
  search,
  statusFilter,
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
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);

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

  return (
    <aside className={open ? "session-rail is-open" : "session-rail"}>
      <header className="rail-header">
        <div className="wordmark" aria-label="Endless Task">
          <span className="wordmark-symbol">∞</span>
          <span>Endless</span>
        </div>
        <button className="icon-button rail-close" onClick={onClose} type="button">
          <span aria-hidden="true">×</span>
          <span className="sr-only">关闭会话列表</span>
        </button>
      </header>

      <button className="new-chat-button" onClick={onNewConversation} type="button">
        <span aria-hidden="true">＋</span>
        新对话
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
        {conversations.length === 0 ? (
          <EmptyState
            className="rail-empty"
            desc={search ? "换个关键词试试。" : "点「新对话」开始，助手会记住你们聊过什么。"}
            title={search ? "没有匹配的对话" : "这里还没有对话"}
          />
        ) : null}
        {conversations.map((conversation) => {
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
