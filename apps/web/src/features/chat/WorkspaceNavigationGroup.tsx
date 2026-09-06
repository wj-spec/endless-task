import { AddIcon, ChevronIcon, FolderIcon } from "../ui/Icons";
import { RowMenu } from "../ui/RowMenu";
import type { SessionActions } from "./workspaceNavigationModel";
import {
  WORKSPACE_VISIBLE_SESSIONS,
  type WorkspaceNavigationGroup as WorkspaceNavigationGroupModel,
} from "./workspaceNavigationModel";
import { WorkspaceSessionList } from "./WorkspaceSessionList";

type WorkspaceNavigationGroupProps = {
  group: WorkspaceNavigationGroupModel;
  activeConversationId: string | null;
  currentWorkspaceId: string | null;
  open: boolean;
  showAll: boolean;
  renamingId: string | null;
  renameDraft: string;
  onToggleOpen: () => void;
  onToggleShowAll: () => void;
  actions: SessionActions;
};

function displayRoot(rootPath: string | null): string {
  if (!rootPath) return "";
  const parts = rootPath.split("/").filter(Boolean);
  return parts.at(-1) ?? rootPath;
}

export function WorkspaceNavigationGroup({
  group,
  activeConversationId,
  currentWorkspaceId,
  open,
  showAll,
  renamingId,
  renameDraft,
  onToggleOpen,
  onToggleShowAll,
  actions,
}: WorkspaceNavigationGroupProps) {
  const unbound = group.rootPath === null;
  const isCurrent = group.id === currentWorkspaceId;
  return (
    <div
      className={`workspace-group${open ? " is-open" : ""}${
        isCurrent ? " is-current" : ""
      }`}
      data-workspace-id={group.id}
    >
      <div className="workspace-item">
        <button
          aria-expanded={open}
          aria-current={isCurrent ? "true" : undefined}
          className="workspace-item-main"
          onClick={onToggleOpen}
          title={group.rootPath ?? group.name}
          type="button"
        >
          <span aria-hidden="true" className="workspace-folder-icon">
            <FolderIcon size={18} />
          </span>
          <span className="workspace-item-name">{group.name}</span>
          <span className={`workspace-item-meta${unbound ? " is-unbound" : ""}`}>
            {unbound ? "未绑定" : displayRoot(group.rootPath)}
          </span>
          <span aria-hidden="true" className="workspace-item-chevron">
            <ChevronIcon direction={open ? "down" : "right"} size={16} />
          </span>
        </button>
        <RowMenu
          className="workspace-row-menu"
          items={[
            ...(unbound && actions.onBindWorkspace
              ? [{ label: "绑定目录…", onSelect: () => actions.onBindWorkspace?.(group.id) }]
              : []),
            ...(actions.onDeleteWorkspace
              ? [
                  {
                    danger: true,
                    label: "删除工作区",
                    onSelect: () => actions.onDeleteWorkspace?.(group.id),
                  },
                ]
              : []),
          ]}
          triggerAriaLabel={`管理工作区：${group.name}`}
          triggerClassName="workspace-menu-button"
        />
        {isCurrent && actions.onNewConversation ? (
          <button
            aria-label="在当前工作区新建会话"
            className="icon-button workspace-new-conversation"
            onClick={() => actions.onNewConversation?.(group.id)}
            title="在当前工作区新建会话"
            type="button"
          >
            <AddIcon size={16} />
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="workspace-conversations">
          {unbound && actions.onBindWorkspace ? (
            <div className="workspace-unbound">
              <p className="workspace-unbound-hint">
                尚未绑定本地目录，绑定后才能新建对话。
              </p>
              <button
                className="workspace-unbound-bind"
                onClick={() => actions.onBindWorkspace?.(group.id)}
                type="button"
              >
                绑定目录…
              </button>
            </div>
          ) : null}
          <WorkspaceSessionList
            actions={actions}
            activeConversationId={activeConversationId}
            conversations={group.conversations}
            emptyDescription={
              unbound
                ? "绑定本地目录后，你才能在这个工作区里新建对话。"
                : "在这个工作区中新建一段对话。"
            }
            emptyTitle="这里还没有对话"
            listClassName="workspace-session-list"
            onToggleShowAll={onToggleShowAll}
            renameDraft={renameDraft}
            renamingId={renamingId}
            showAll={showAll}
            visibleCount={WORKSPACE_VISIBLE_SESSIONS}
          />
        </div>
      ) : null}
    </div>
  );
}