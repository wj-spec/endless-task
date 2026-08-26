import { Fragment, useEffect, useMemo, useState } from "react";
import type { Conversation, ConversationStatus } from "./apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { EmptyState } from "../ui/EmptyState";
import { RowMenu } from "../ui/RowMenu";

type SessionRailProps = {
  activeConversationId: string | null;
  conversations: Conversation[];
  open: boolean;
  search: string;
  sideConversationId: string | null;
  statusFilter: ConversationStatus;
  onChangeConversationStatus: (
    conversationId: string,
    status: ConversationStatus,
  ) => void;
  onClose: () => void;
  onDeleteConversation: (conversationId: string) => void;
  onNewConversation: () => void;
  onPromoteConversation: (conversationId: string) => void;
  onRenameConversation: (conversationId: string, title: string) => void;
  onSearchChange: (value: string) => void;
  onSelectBranch: (branchId: string, parentId: string) => void;
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
  sideConversationId,
  statusFilter,
  onChangeConversationStatus,
  onClose,
  onDeleteConversation,
  onNewConversation,
  onPromoteConversation,
  onRenameConversation,
  onSearchChange,
  onSelectBranch,
  onSelectConversation,
  onStatusFilterChange,
}: SessionRailProps) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [expandedParentId, setExpandedParentId] = useState<string | null>(null);

  const childMap = useMemo(() => {
    const map = new Map<string, Conversation[]>();
    for (const item of conversations) {
      if (item.kind !== "ephemeral" || !item.parentConversationId) continue;
      const list = map.get(item.parentConversationId) ?? [];
      list.push(item);
      map.set(item.parentConversationId, list);
    }
    return map;
  }, [conversations]);
  const primaryConversations = conversations.filter(
    (item) => item.kind !== "ephemeral",
  );

  useEffect(() => {
    if (!sideConversationId) return;
    for (const [parentId, children] of childMap.entries()) {
      if (children.some((item) => item.id === sideConversationId)) {
        setExpandedParentId(parentId);
        return;
      }
    }
  }, [sideConversationId, childMap]);

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
        {primaryConversations.map((conversation) => {
          const isActive = conversation.id === activeConversationId;
          const archived = conversation.status === "archived";
          const children = childMap.get(conversation.id) ?? [];
          const expanded = expandedParentId === conversation.id;
          return (
            <Fragment key={conversation.id}>
            <div
              className={isActive ? "session-item is-active" : "session-item"}
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
              {children.length > 0 ? (
                <button
                  aria-expanded={expanded}
                  className={expanded ? "branch-badge is-open" : "branch-badge"}
                  onClick={() =>
                    setExpandedParentId(expanded ? null : conversation.id)
                  }
                  type="button"
                >
                  {children.length} 临时
                </button>
              ) : null}
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
            {expanded && children.length > 0 ? (
              <div className="branch-children">
                {children.map((child) => {
                  const childActive =
                    child.id === activeConversationId ||
                    child.id === sideConversationId;
                  return (
                    <div
                      className={
                        childActive ? "branch-child is-active" : "branch-child"
                      }
                      key={child.id}
                    >
                      <button
                        className="branch-child-main"
                        onClick={() => onSelectBranch(child.id, conversation.id)}
                        type="button"
                      >
                        <span aria-hidden="true">↳</span>
                        <span className="branch-child-title">{child.title}</span>
                        <time
                          className="branch-child-time"
                          dateTime={child.updatedAt}
                        >
                          {formatRelativeTime(child.updatedAt)}
                        </time>
                      </button>
                      <RowMenu
                        className="branch-row-menu"
                        items={[
                          {
                            label: "升级为正式对话",
                            onSelect: () => onPromoteConversation(child.id),
                          },
                          {
                            danger: true,
                            label: "删除",
                            onSelect: () => setDeleteTarget(child),
                          },
                        ]}
                        triggerAriaLabel={`管理分支：${child.title}`}
                        triggerClassName="session-menu-button"
                      />
                    </div>
                  );
                })}
              </div>
            ) : null}
            </Fragment>
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
