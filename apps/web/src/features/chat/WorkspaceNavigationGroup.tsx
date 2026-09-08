import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  homePath: string;
  open: boolean;
  showAll: boolean;
  renamingId: string | null;
  renameDraft: string;
  onToggleOpen: () => void;
  onToggleShowAll: () => void;
  actions: SessionActions;
};

function formatCreatedDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `创建于${y}年${m}月${d}日`;
}

/** 把绝对路径里<宿主机根目录>前缀替换成 ~，例如 /Users/endless/… → ~/…。 */
export function displayRootPath(rootPath: string, homePath: string): string {
  if (!homePath) return rootPath;
  const home = homePath.replace(/\/+$/, "");
  if (rootPath === home) return "~";
  if (rootPath.startsWith(`${home}/`)) return `~${rootPath.slice(home.length)}`;
  return rootPath;
}

const HOVER_GAP = 8;

export function WorkspaceNavigationGroup({
  group,
  activeConversationId,
  currentWorkspaceId,
  homePath,
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
  const itemRef = useRef<HTMLDivElement>(null);
  const infoRef = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number }>({
    left: 0,
    top: 0,
  });

  // 触发行位置 → 浮层坐标：固定浮在行右侧（占据内容区），仅做上下视口收拢。
  const computePosition = () => {
    const anchor = itemRef.current;
    const info = infoRef.current;
    if (!anchor || !info) return;
    const rect = anchor.getBoundingClientRect();
    const infoRect = info.getBoundingClientRect();
    const gap = HOVER_GAP;
    const left = rect.right + gap;
    let top = rect.top;
    if (top + infoRect.height > window.innerHeight - gap) {
      top = window.innerHeight - gap - infoRect.height;
    }
    top = Math.max(gap, top);
    setPosition({ left, top });
  };

  // 浮层 DOM 挂载后（portal）再用 useLayoutEffect 精确测量定位，
  // 避免首帧仍停留在 (0,0)。副作用循环依赖 item/info 尺寸。
  const hovering = hovered;
  useLayoutEffect(() => {
    if (!hovering) return;
    computePosition();
    const onResize = () => computePosition();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hovering, group.id]);

  return (
    <div
      className={`workspace-group${open ? " is-open" : ""}${
        isCurrent ? " is-current" : ""
      }`}
      data-workspace-id={group.id}
    >
      <div
        className="workspace-item"
        ref={itemRef}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node)) {
            setHovered(false);
          }
        }}
        onFocus={() => setHovered(true)}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        <button
          aria-expanded={open}
          aria-current={isCurrent ? "true" : undefined}
          className="workspace-item-main"
          onClick={onToggleOpen}
          type="button"
        >
          <span aria-hidden="true" className="workspace-folder-icon">
            <FolderIcon size={18} />
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
      {hovered
        ? createPortal(
            <div
              className="workspace-hover-info"
              ref={infoRef}
              role="tooltip"
              style={{ left: position.left, top: position.top }}
            >
              <span className="workspace-hover-name">{group.name}</span>
              {group.rootPath ? (
                <span className="workspace-hover-path" title={group.rootPath}>
                  {displayRootPath(group.rootPath, homePath)}
                </span>
              ) : (
                <span className="workspace-hover-path">未绑定本地目录</span>
              )}
              <span className="workspace-hover-created">
                {formatCreatedDate(group.createdAt)}
              </span>
            </div>,
            document.body,
          )
        : null}
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
