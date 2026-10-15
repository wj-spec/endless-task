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
  const ephemeral = conversation.kind === "ephemeral";
  // 左栏标识（P0-B）：临时对话"关闭即丢弃"、分支是从别的对话分出来的——两者的
  // 可见语义都不等于普通对话，而此前这行只渲染标题，看起来完全一样。
  // 数据取自既有 payload（`apiTypes.ts` 的 `Conversation.kind` /
  // `parentConversationId`），不新增后端字段。
  // 优先级：临时对话优先——它承载"会被丢弃、不写入记忆"这条最不可逆的信息；分支身份
  // 另有继承摘要条（`LineageSummary`）说明来源，缺失信息更少。
  const badge = ephemeral
    ? { label: "临时", title: "临时对话：丢弃后不可恢复，不写入记忆" }
    : conversation.parentConversationId != null
      ? { label: "分支", title: "分支对话：从另一条对话分出的路径" }
      : null;
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
          <span className="session-item-title">{title}</span>
          {badge ? (
            <span className="session-item-badge" title={badge.title}>
              {badge.label}
            </span>
          ) : null}
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
            // 临时对话在左栏的行菜单里也要说清后果：它和普通对话一样是"销毁"，
            // 但不说清就会看起来像普通的"删除对话"（那个是"永久删除对话"）。
            label: ephemeral ? "丢弃临时对话" : "删除",
            onSelect: onDelete,
          },
        ]}
        triggerAriaLabel={`管理对话：${title}`}
        triggerClassName="session-menu-button"
      />
    </div>
  );
}