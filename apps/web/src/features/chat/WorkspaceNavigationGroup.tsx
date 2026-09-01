import { ChevronIcon, FolderIcon } from "../ui/Icons";
import type { SessionActions } from "./workspaceNavigationModel";
import {
  WORKSPACE_VISIBLE_SESSIONS,
  type WorkspaceNavigationGroup as WorkspaceNavigationGroupModel,
} from "./workspaceNavigationModel";
import { WorkspaceSessionList } from "./WorkspaceSessionList";

type WorkspaceNavigationGroupProps = {
  group: WorkspaceNavigationGroupModel;
  activeConversationId: string | null;
  open: boolean;
  showAll: boolean;
  renamingId: string | null;
  renameDraft: string;
  onToggleOpen: () => void;
  onToggleShowAll: () => void;
  actions: SessionActions;
};

export function WorkspaceNavigationGroup({
  group,
  activeConversationId,
  open,
  showAll,
  renamingId,
  renameDraft,
  onToggleOpen,
  onToggleShowAll,
  actions,
}: WorkspaceNavigationGroupProps) {
  const unbound = group.rootPath === null;
  return (
    <div
      className={`workspace-group${open ? " is-open" : ""}`}
      data-workspace-id={group.id}
    >
      <button
        aria-expanded={open}
        className="workspace-item-main"
        onClick={onToggleOpen}
        title={group.rootPath ?? group.name}
        type="button"
      >
        <span aria-hidden="true" className="workspace-folder-icon">
          <FolderIcon size={18} />
        </span>
        <span className="workspace-item-name">{group.name}</span>
        <span aria-hidden="true" className="workspace-item-chevron">
          <ChevronIcon direction={open ? "down" : "right"} size={16} />
        </span>
      </button>
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