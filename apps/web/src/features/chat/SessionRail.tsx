import type {
  Conversation,
  ConversationStatus,
  HealthSnapshot,
} from "./apiTypes";

type SessionRailProps = {
  activeConversationId: string | null;
  conversations: Conversation[];
  health: HealthSnapshot | null;
  open: boolean;
  search: string;
  statusFilter: ConversationStatus;
  onClose: () => void;
  onNewConversation: () => void;
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
  health,
  open,
  search,
  statusFilter,
  onClose,
  onNewConversation,
  onSearchChange,
  onSelectConversation,
  onStatusFilterChange,
}: SessionRailProps) {
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
          <p className="rail-empty">
            {search ? "没有匹配的对话" : "这里还没有对话"}
          </p>
        ) : null}
        {conversations.map((conversation) => (
          <button
            aria-current={conversation.id === activeConversationId ? "page" : undefined}
            className={
              conversation.id === activeConversationId
                ? "session-item is-active"
                : "session-item"
            }
            key={conversation.id}
            onClick={() => onSelectConversation(conversation.id)}
            type="button"
          >
            <span>{conversation.title}</span>
            <time dateTime={conversation.updatedAt}>
              {formatRelativeTime(conversation.updatedAt)}
            </time>
          </button>
        ))}
      </nav>

      <footer className="rail-footer">
        <span
          className={health?.providerConfigured ? "status-light" : "status-light is-warning"}
        />
        <div>
          <span>{health ? `${health.provider} · ${health.model}` : "本地服务未连接"}</span>
          <small>{health?.providerConfigured ? "仅在本机运行" : "需要配置模型服务"}</small>
        </div>
      </footer>
    </aside>
  );
}
