import type { Conversation, ConversationStatus } from "./apiTypes";
import { RowMenu } from "../ui/RowMenu";
import { conversationTitle } from "./workspaceNavigationModel";

type SessionRowProps = {
  conversation: Conversation;
  active: boolean;
  renaming: boolean;
  renameDraft: string;
  onRenameStart: () => void;
  onRenameDraftChange: (value: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
  onSelect: () => void;
  onChangeStatus: (status: ConversationStatus) => void;
  onDelete: () => void;
};

export function SessionRow({
  conversation,
  active,
  renaming,
  renameDraft,
  onRenameStart,
  onRenameDraftChange,
  onRenameCommit,
  onRenameCancel,
  onSelect,
  onChangeStatus,
  onDelete,
}: SessionRowProps) {
  const archived = conversation.status === "archived";
  const title = conversationTitle(conversation);
  return (
    <div
      className={active ? "session-item is-active" : "session-item"}
      data-conversation-id={conversation.id}
    >
      {renaming ? (
        <div className="session-rename">
          <input
            aria-label="对话名称"
            autoFocus
            onBlur={onRenameCommit}
            onChange={(event) => onRenameDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onRenameCommit();
              if (event.key === "Escape") onRenameCancel();
            }}
            value={renameDraft}
          />
        </div>
      ) : (
        <button
          aria-current={active ? "page" : undefined}
          className="session-item-main"
          onClick={onSelect}
          type="button"
        >
          <span>{title}</span>
        </button>
      )}
      <RowMenu
        className="session-row-menu"
        items={[
          { label: "重命名", onSelect: onRenameStart },
          {
            label: archived ? "取消归档" : "归档",
            onSelect: () => onChangeStatus(archived ? "active" : "archived"),
          },
          {
            danger: true,
            label: "删除",
            onSelect: onDelete,
          },
        ]}
        triggerAriaLabel={`管理对话：${title}`}
        triggerClassName="session-menu-button"
      />
    </div>
  );
}