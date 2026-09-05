import type { ConversationSnapshot, RuntimeV2Lane, Workspace } from "./apiTypes";
import { BranchNavigator } from "./BranchNavigator";
import { CloseIcon, FolderIcon, MoreIcon, SearchIcon } from "../ui/Icons";
import { RowMenu } from "../ui/RowMenu";
import { SidebarToggleIcon } from "../ui/SidebarToggleIcon";

type ChatSurfaceHeaderProps = {
  activeWorkspace?: Workspace | null;
  archived: boolean;
  branchLanes: RuntimeV2Lane[];
  conversation: ConversationSnapshot | null;
  currentLaneId: string | null;
  currentLaneLabel: string;
  isGenerating: boolean;
  pendingAction: string | null;
  renaming: boolean;
  sideMode: "temporary_conversation" | "branch_lane" | null;
  titleDraft: string;
  variant: "main" | "side";
  onArchive: () => void;
  onArchiveLane?: (laneId: string, includeArchived: boolean) => void;
  onCreateBranch?: (forkTurnId: string) => void;
  onCreateTemporaryConversation?: () => void;
  onMenu: () => void;
  onOpenLaneInSide?: (laneId: string) => void;
  onOpenWorkspaceSettings?: () => void;
  onPromoteLane?: (laneId: string) => void;
  onRenameLane?: (laneId: string, displayName: string | null) => void;
  onRequestSideClose: () => void;
  onRestore: () => void;
  onRestoreLane?: (laneId: string) => void;
  onSearch: () => void;
  onGlobalSearch?: () => void;
  onSetConfirmingDelete: () => void;
  onSetRenaming: (renaming: boolean) => void;
  onShowArchivedLanes?: (visible: boolean) => void | Promise<void>;
  onSwitchLane?: (laneId: string) => void;
  onSubmitTitle: () => void;
  onTitleDraftChange: (title: string) => void;
};

export function ChatSurfaceHeader({
  activeWorkspace,
  archived,
  branchLanes,
  conversation,
  currentLaneId,
  currentLaneLabel,
  isGenerating,
  pendingAction,
  renaming,
  sideMode,
  titleDraft,
  variant,
  onArchive,
  onArchiveLane,
  onCreateBranch,
  onCreateTemporaryConversation,
  onMenu,
  onOpenLaneInSide,
  onOpenWorkspaceSettings,
  onPromoteLane,
  onRenameLane,
  onRequestSideClose,
  onRestore,
  onRestoreLane,

    onGlobalSearch,

  onSearch,
  onSetConfirmingDelete,
  onSetRenaming,
  onShowArchivedLanes,
  onSwitchLane,
  onSubmitTitle,
  onTitleDraftChange,
}: ChatSurfaceHeaderProps) {
  if (variant === "side") {
    return (
      <header className="surface-header side-surface-header">
        <div className="conversation-heading">
          <h2>
            {sideMode === "branch_lane"
              ? currentLaneLabel
              : (conversation?.conversation.title ?? "临时对话")}
          </h2>
          {sideMode === "temporary_conversation" ? (
            <span className="temporary-close-hint">关闭即删除</span>
          ) : sideMode === "branch_lane" ? (
            <span className="temporary-close-hint">关闭仅收起，不删除分支</span>
          ) : null}
        </div>
        <div className="surface-header-side">
          <button
            aria-label={
              sideMode === "branch_lane" ? "收起分支对照" : "关闭并删除临时对话"
            }
            className="icon-button side-close"
            onClick={onRequestSideClose}
            type="button"
          >
            <CloseIcon size={20} />
          </button>
        </div>
      </header>
    );
  }

  const conversationId = conversation?.conversation.id;
  const canNavigateBranches =
    conversationId &&
    branchLanes.length > 0 &&
    onCreateBranch &&
    onArchiveLane &&
    onOpenLaneInSide &&
    onPromoteLane &&
    onRenameLane &&
    onRestoreLane &&
    onShowArchivedLanes &&
    onSwitchLane;

  return (
    <header className="surface-header">
      <button
        aria-label="展开侧栏"
        className="icon-button mobile-menu"
        onClick={onMenu}
        title="展开侧栏"
        type="button"
      >
        <SidebarToggleIcon expanded={false} />
      </button>
      <div className="surface-header-frame">
        <div className="conversation-heading">
          {renaming ? (
            <input
              aria-label="会话标题"
              autoFocus
              className="title-input"
              onBlur={onSubmitTitle}
              onChange={(event) => onTitleDraftChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onSubmitTitle();
                if (event.key === "Escape") onSetRenaming(false);
              }}
              value={titleDraft}
            />
          ) : activeWorkspace ? (
            <div className="heading-context">
              <span className="heading-focus">
                <span aria-hidden="true" className="heading-focus-icon">
                  <FolderIcon size={15} />
                </span>
                <span className="heading-focus-name">{activeWorkspace.name}</span>
              </span>
              <h1 className="heading-sub">
                {conversation?.conversation.title ?? "Endless"}
              </h1>
            </div>
          ) : (
            <h1 className="heading-title">
              {conversation?.conversation.title ?? "Endless"}
            </h1>
          )}
          {archived ? <span className="archived-chip">已归档</span> : null}
        {canNavigateBranches ? (
          <BranchNavigator
            conversationId={conversationId}
            currentLaneId={currentLaneId}
            disabled={isGenerating || pendingAction !== null}
            lanes={branchLanes}
            onArchive={onArchiveLane}
            onOpenSide={onOpenLaneInSide}
            onPromote={onPromoteLane}
            onRename={onRenameLane}
            onRestore={onRestoreLane}
            onShowArchived={onShowArchivedLanes}
            onSwitch={onSwitchLane}
          />
        ) : null}
      </div>
      <div className="surface-header-side">
        {conversation ? (
          <div className="conversation-actions">
            {onGlobalSearch ? (
              <button
                aria-label="全局搜索"
                className="icon-button conversation-icon-button"
                onClick={onGlobalSearch}
                title="搜索所有会话、知识、记忆与成果"
                type="button"
              >
                <SearchIcon size={19} />
              </button>
            ) : null}
            <button
              aria-label="搜索当前会话"
              className="icon-button conversation-icon-button"
              onClick={onSearch}
              title="搜索当前会话"
              type="button"
            >
              <SearchIcon size={19} />
            </button>
            <RowMenu
              trigger={<MoreIcon size={20} />}
              triggerAriaLabel="更多会话操作"
              triggerClassName="icon-button conversation-icon-button"
              items={[
                { label: "重命名", onSelect: () => onSetRenaming(true) },
                ...(onOpenWorkspaceSettings
                  ? [
                      {
                        label: "配置当前工作区",
                        onSelect: onOpenWorkspaceSettings,
                      },
                    ]
                  : []),
                {
                  disabled:
                    isGenerating ||
                    pendingAction !== null ||
                    !onCreateTemporaryConversation ||
                    conversation.turns.length === 0,
                  label: "从主线创建临时对话",
                  onSelect: () => onCreateTemporaryConversation?.(),
                },
                {
                  label: archived ? "恢复" : "归档",
                  disabled: isGenerating,
                  onSelect: archived ? onRestore : onArchive,
                },
                {
                  danger: true,
                  disabled: isGenerating,
                  label: "删除",
                  onSelect: onSetConfirmingDelete,
                },
              ]}
            />
          </div>
        ) : null}
      </div>
      </div>
    </header>
  );
}