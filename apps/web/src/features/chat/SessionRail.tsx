import { useEffect, useMemo, useRef, useState } from "react";
import type {
  Conversation,
  ConversationStatus,
  Workspace,
} from "./apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { AddIcon, SearchIcon } from "../ui/Icons";
import { SidebarToggleIcon } from "../ui/SidebarToggleIcon";
import { useModalDialog } from "../ui/useModalDialog";
import { RecentSessionGroup } from "./RecentSessionGroup";
import { WorkspaceNavigationGroup } from "./WorkspaceNavigationGroup";
import { useWorkspaceNavigation } from "./useWorkspaceNavigation";
import {
  buildWorkspaceNavigationModel,
  conversationTitle,
  type SessionActions,
} from "./workspaceNavigationModel";

type SessionRailProps = {
  activeConversationId: string | null;
  collapsed: boolean;
  open: boolean;
  search: string;
  statusFilter: ConversationStatus;
  workspaceId: string | null;
  workspaces: Workspace[];
  workspaceCanCreate: boolean;
  currentWorkspace: Workspace | null;
  pendingTotal: number;
  onCreateWorkspace: () => void;
  onOpenAssistant: (tab: "notifications" | "memory") => void;
  onOpenSettings: () => void;
  onRequestBindWorkspace: (workspaceId: string) => void;
  onChangeConversationStatus: (
    conversationId: string,
    status: ConversationStatus,
  ) => void;
  onClose: () => void;
  onDeleteConversation: (conversationId: string) => void;
  onDeleteWorkspace: (workspaceId: string) => Promise<void>;
  onNewConversation: () => void;
  onRenameConversation: (conversationId: string, title: string) => void;
  onSearchChange: (value: string) => void;
  onSelectConversation: (conversationId: string) => void;
  onStatusFilterChange: (status: ConversationStatus) => void;
};

export function SessionRail({
  activeConversationId,
  collapsed,
  open,
  search,
  statusFilter,
  workspaceId,
  workspaces,
  workspaceCanCreate,
  currentWorkspace,
  pendingTotal,
  onCreateWorkspace,
  onOpenAssistant,
  onOpenSettings,
  onRequestBindWorkspace,
  onChangeConversationStatus,
  onClose,
  onDeleteConversation,
  onDeleteWorkspace,
  onNewConversation,
  onRenameConversation,
  onSearchChange,
  onSelectConversation,
  onStatusFilterChange,
}: SessionRailProps) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [deleteWorkspaceTarget, setDeleteWorkspaceTarget] = useState<
    Workspace | null
  >(null);
  const [deleteWorkspaceError, setDeleteWorkspaceError] = useState<string | null>(
    null,
  );
  const [deletingWorkspace, setDeletingWorkspace] = useState(false);
  const createWorkspaceButtonRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const railRef = useModalDialog<HTMLElement>({
    active: open,
    initialFocusRef: closeButtonRef,
    onClose,
  });

  const nav = useWorkspaceNavigation(workspaces, statusFilter, search);

  const model = useMemo(
    () =>
      buildWorkspaceNavigationModel({
        workspaces,
        byWorkspace: nav.byWorkspace,
        general: nav.general,
      }),
    [workspaces, nav.byWorkspace, nav.general],
  );

  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const [showAllGroups, setShowAllGroups] = useState<Set<string>>(
    () => new Set(),
  );
  const [recentShowAll, setRecentShowAll] = useState(false);
  const previousActiveWorkspaceIdRef = useRef<string | null>(null);
  const sessionNavRef = useRef<HTMLElement | null>(null);

  // 待删工作区的会话数：用于删除确认的醒目提示。
  const deleteWorkspaceConversationCount = useMemo(() => {
    if (!deleteWorkspaceTarget) return 0;
    return (
      model.groups.find((group) => group.id === deleteWorkspaceTarget.id)
        ?.totalCount ?? 0
    );
  }, [deleteWorkspaceTarget, model.groups]);

  // 当前工作区：优先取活动会话所属工作区，否则用侧栏工作区上下文。
  const currentWorkspaceId = useMemo(() => {
    if (activeConversationId) {
      const activeGroup = model.groups.find((group) =>
        group.conversations.some((item) => item.id === activeConversationId),
      );
      if (activeGroup) return activeGroup.id;
    }
    return workspaceId;
  }, [activeConversationId, model.groups, workspaceId]);

  useEffect(() => {
    const targetGroup =
      (activeConversationId
        ? model.groups.find((group) =>
            group.conversations.some((item) => item.id === activeConversationId),
          )
        : undefined) ?? model.groups.find((group) => group.id === workspaceId);

    const nextWorkspaceId = targetGroup?.id ?? null;
    const previousWorkspaceId = previousActiveWorkspaceIdRef.current;

    // 仅当活动会话所属工作区（或工作区上下文）真正切换时才自动展开目标组并让选中行可见；
    // 工作区身份未变（含用户手动收起后 active 未变）时保持现状，避免被反复强制展开造成状态循环。
    if (nextWorkspaceId !== null && nextWorkspaceId !== previousWorkspaceId) {
      setOpenGroups((prev) => {
        const next = new Set(prev);
        next.add(nextWorkspaceId);
        return next;
      });
      if (activeConversationId) {
        requestAnimationFrame(() => {
          sessionNavRef.current
            ?.querySelector<HTMLElement>(
              `[data-conversation-id="${activeConversationId}"]`,
            )
            ?.scrollIntoView({ block: "nearest" });
        });
      }
    }

    previousActiveWorkspaceIdRef.current = nextWorkspaceId;
  }, [activeConversationId, model, workspaceId]);

  const toggleGroupOpen = (workspaceGroupId: string) => {
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(workspaceGroupId)) next.delete(workspaceGroupId);
      else next.add(workspaceGroupId);
      return next;
    });
  };
  const toggleGroupShowAll = (workspaceGroupId: string) => {
    setShowAllGroups((prev) => {
      const next = new Set(prev);
      if (next.has(workspaceGroupId)) next.delete(workspaceGroupId);
      else next.add(workspaceGroupId);
      return next;
    });
  };

  const findConversation = (conversationId: string): Conversation | null => {
    for (const group of model.groups) {
      const found = group.conversations.find((item) => item.id === conversationId);
      if (found) return found;
    }
    return model.recent.find((item) => item.id === conversationId) ?? null;
  };

  const startRename = (conversationId: string) => {
    const conversation = findConversation(conversationId);
    setRenamingId(conversationId);
    setRenameDraft(conversation ? conversationTitle(conversation) : "");
  };
  const commitRename = () => {
    if (!renamingId) return;
    const title = renameDraft.trim();
    if (title) onRenameConversation(renamingId, title);
    setRenamingId(null);
    nav.refresh();
  };

  const actions: SessionActions = {
    onSelectConversation: (conversationId) => {
      onSelectConversation(conversationId);
    },
    onRenameStart: startRename,
    onRenameDraftChange: setRenameDraft,
    onRenameCommit: commitRename,
    onRenameCancel: () => setRenamingId(null),
    onChangeStatus: (conversationId, status) => {
      onChangeConversationStatus(conversationId, status);
      nav.refresh();
    },
    onDelete: (conversationId) => {
      const conversation = findConversation(conversationId);
      if (conversation) setDeleteTarget(conversation);
    },
    onBindWorkspace: (workspaceId) => onRequestBindWorkspace(workspaceId),
    onDeleteWorkspace: (workspaceId) => {
      setDeleteWorkspaceError(null);
      const workspace = workspaces.find((item) => item.id === workspaceId);
      if (workspace) setDeleteWorkspaceTarget(workspace);
    },
  };

  const railExpanded = open || !collapsed;
  const railToggleLabel = railExpanded ? "收起侧栏" : "展开侧栏";

  return (
    <aside
      aria-label="会话导航"
      aria-modal={open ? true : undefined}
      className={`session-rail${open ? " is-open" : ""}${
        collapsed ? " is-collapsed" : ""
      }`}
      ref={railRef}
      role={open ? "dialog" : undefined}
      tabIndex={open ? -1 : undefined}
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
          ref={closeButtonRef}
          title={railToggleLabel}
          type="button"
        >
          <SidebarToggleIcon expanded={railExpanded} />
        </button>
      </header>

      <button
        aria-disabled={!workspaceCanCreate}
        aria-label="新对话"
        className="new-chat-button"
        disabled={!workspaceCanCreate}
        onClick={onNewConversation}
        title={
          !workspaceCanCreate
            ? currentWorkspace
              ? "当前工作区尚未绑定本地目录，请先绑定目录后再新建对话"
              : "请先选择并绑定一个工作区目录，再新建对话"
            : collapsed
              ? "新对话"
              : undefined
        }
        type="button"
      >
        <span aria-hidden="true" className="new-chat-icon">
          <AddIcon size={20} />
        </span>
        <span className="new-chat-label">新对话</span>
      </button>

      <label className="conversation-search">
        <span className="sr-only">搜索对话</span>
        <SearchIcon size={17} />
        <input
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="搜索对话"
          type="search"
          value={search}
        />
      </label>

      <div className="rail-filter" role="tablist" aria-label="会话状态">
        {(["active", "archived"] as const).map((status) => (
          <button
            aria-selected={statusFilter === status}
            className={statusFilter === status ? "is-active" : ""}
            key={status}
            onClick={() => onStatusFilterChange(status)}
            role="tab"
            type="button"
          >
            {status === "active" ? "进行中" : "已归档"}
          </button>
        ))}
      </div>

      <section aria-label="工作区" className="workspace-navigation">
        <div className="workspace-navigation-header">
          <span>工作区</span>
          <button
            aria-label="新建工作区"
            className="icon-button rail-create-workspace"
            onClick={onCreateWorkspace}
            ref={createWorkspaceButtonRef}
            title="新建工作区"
            type="button"
          >
            <AddIcon size={18} />
          </button>
        </div>
        <nav
          aria-label="会话列表"
          className="session-navigation"
          ref={sessionNavRef}
        >
          {model.groups.map((group) => (
            <WorkspaceNavigationGroup
              actions={actions}
              activeConversationId={activeConversationId}
              currentWorkspaceId={currentWorkspaceId}
              group={group}
              key={group.id}
              onToggleOpen={() => toggleGroupOpen(group.id)}
              onToggleShowAll={() => toggleGroupShowAll(group.id)}
              open={openGroups.has(group.id)}
              renameDraft={renameDraft}
              renamingId={renamingId}
              showAll={showAllGroups.has(group.id)}
            />
          ))}
          <RecentSessionGroup
            actions={actions}
            activeConversationId={activeConversationId}
            conversations={model.recent}
            onToggleShowAll={() => setRecentShowAll((current) => !current)}
            renameDraft={renameDraft}
            renamingId={renamingId}
            showAll={recentShowAll}
          />
        </nav>
      </section>

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
          onConfirm={() => {
            onDeleteConversation(deleteTarget.id);
            nav.refresh();
          }}
          title="删除这段对话？"
        />
      ) : null}
      {deleteWorkspaceTarget ? (
        <ConfirmDialog
          tone="danger"
          warning={
            deleteWorkspaceConversationCount > 0
              ? `该工作区下有 ${deleteWorkspaceConversationCount} 个会话，删除后将一并删除，无法恢复。`
              : undefined
          }
          body={
            deleteWorkspaceTarget.rootPath
              ? `将删除工作区「${deleteWorkspaceTarget.name}」${
                  deleteWorkspaceConversationCount > 0
                    ? `及名下 ${deleteWorkspaceConversationCount} 个会话`
                    : ""
                }。本地目录与已存文件不会被删除。`
              : `将删除尚未绑定目录的工作区「${deleteWorkspaceTarget.name}」${
                  deleteWorkspaceConversationCount > 0
                    ? `及名下 ${deleteWorkspaceConversationCount} 个会话`
                    : ""
                }。`
          }
          confirmLabel={deletingWorkspace ? "删除中…" : "删除工作区"}
          onClose={() => {
            setDeleteWorkspaceTarget(null);
            setDeleteWorkspaceError(null);
          }}
          onConfirm={() => {
            setDeletingWorkspace(true);
            setDeleteWorkspaceError(null);
            void onDeleteWorkspace(deleteWorkspaceTarget.id)
              .then(() => {
                setDeleteWorkspaceTarget(null);
                setDeleteWorkspaceError(null);
                nav.refresh();
              })
              .catch((error) => {
                setDeleteWorkspaceError(
                  error instanceof Error ? error.message : "删除工作区失败，请重试。",
                );
              })
              .finally(() => setDeletingWorkspace(false));
          }}
          title="删除这个工作区？"
        />
      ) : null}
      {deleteWorkspaceError ? (
        <p className="rail-error" role="alert">
          {deleteWorkspaceError}
        </p>
      ) : null}
    </aside>
  );
}