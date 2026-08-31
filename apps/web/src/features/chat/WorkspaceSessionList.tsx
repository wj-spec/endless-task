import { useId } from "react";
import type { Conversation } from "./apiTypes";
import { EmptyState } from "../ui/EmptyState";
import type { SessionActions } from "./workspaceNavigationModel";
import { SessionRow } from "./SessionRow";

type WorkspaceSessionListProps = {
  conversations: Conversation[];
  activeConversationId: string | null;
  visibleCount: number;
  showAll: boolean;
  renamingId: string | null;
  renameDraft: string;
  emptyTitle: string;
  emptyDescription: string;
  listClassName?: string;
  onToggleShowAll: () => void;
  actions: SessionActions;
};

export function WorkspaceSessionList({
  conversations,
  activeConversationId,
  visibleCount,
  showAll,
  renamingId,
  renameDraft,
  emptyTitle,
  emptyDescription,
  listClassName = "",
  onToggleShowAll,
  actions,
}: WorkspaceSessionListProps) {
  const listId = useId();
  const hiddenCount = conversations.length - visibleCount;

  if (conversations.length === 0) {
    return (
      <EmptyState
        className="rail-empty"
        desc={emptyDescription}
        title={emptyTitle}
      />
    );
  }

  const visible = showAll ? conversations : conversations.slice(0, visibleCount);

  return (
    <div className={`session-list ${listClassName}`.trim()}>
      <div id={listId}>
        {visible.map((conversation) => (
          <SessionRow
            active={conversation.id === activeConversationId}
            conversation={conversation}
            key={conversation.id}
            onDelete={() => actions.onDelete(conversation.id)}
            onChangeStatus={(status) =>
              actions.onChangeStatus(conversation.id, status)
            }
            onRenameCancel={actions.onRenameCancel}
            onRenameCommit={actions.onRenameCommit}
            onRenameDraftChange={actions.onRenameDraftChange}
            onRenameStart={() => actions.onRenameStart(conversation.id)}
            onSelect={() => actions.onSelectConversation(conversation.id)}
            renameDraft={renameDraft}
            renaming={renamingId === conversation.id}
          />
        ))}
      </div>
      {!showAll && hiddenCount > 0 ? (
        <button
          aria-controls={listId}
          aria-expanded={false}
          className="session-expand"
          onClick={onToggleShowAll}
          type="button"
        >
          展开显示（{hiddenCount}）
        </button>
      ) : null}
      {showAll && conversations.length > visibleCount ? (
        <button
          aria-controls={listId}
          aria-expanded={true}
          className="session-expand"
          onClick={onToggleShowAll}
          type="button"
        >
          收起
        </button>
      ) : null}
    </div>
  );
}