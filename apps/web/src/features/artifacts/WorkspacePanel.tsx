import { useEffect, useId, useRef, useState } from "react";
import { chatApi } from "../chat/api";
import type { WorkspaceSnapshot } from "../chat/apiTypes";
import { WorkspaceFilesPane } from "../files/WorkspaceFilesPane";
import { TerminalWorkspace } from "../terminal/TerminalPane";
import { useTerminalSessions } from "../terminal/useTerminalSessions";
import { BrowserPlaceholder } from "../workspace/views/BrowserPlaceholder";
import { WorkspaceViewTabs } from "../workspace/views/WorkspaceViewTabs";
import { useWorkspaceViews } from "../workspace/views/useWorkspaceViews";
import { ArtifactDetail } from "./ArtifactDetail";
import { formatRelativeTime } from "./time";
import { useMediaQuery } from "../ui/useMediaQuery";
import { useModalDialog } from "../ui/useModalDialog";

type WorkspacePanelProps = {
  workspace: WorkspaceSnapshot;
  workspaceId: string;
  conversationId: string;
  latestTurnId: string | null;
  drawerOpen: boolean;
  onCollapse: () => void;
  onWorkspaceRefresh: () => void;
  workspaceRootPath?: string | null;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

export function WorkspacePanel({
  workspace,
  workspaceId,
  conversationId,
  latestTurnId,
  drawerOpen,
  onCollapse,
  onWorkspaceRefresh,
  workspaceRootPath,
}: WorkspacePanelProps) {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const views = useWorkspaceViews(conversationId);
  const tab = views.activeKind;
  const terminals = useTerminalSessions(tab === "terminals" ? workspaceId : "");
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const isMobile = useMediaQuery("(max-width: 760px)");
  const isDrawerModal = drawerOpen && isMobile;
  const drawerDialogRef = useModalDialog<HTMLElement>({
    active: isDrawerModal,
    initialFocusRef: closeButtonRef,
    onClose: onCollapse,
  });

  useEffect(() => {
    setSelectedArtifactId(null);
  }, [workspace.conversationId]);

  useEffect(() => {
    if (
      selectedArtifactId &&
      !workspace.artifacts.some((item) => item.id === selectedArtifactId)
    ) {
      setSelectedArtifactId(null);
    }
  }, [workspace.artifacts, selectedArtifactId]);

  const pendingCount = workspace.pendingProposals.length;
  // 面包屑首段：优先用绑定目录名，其次"工作区"。
  const workspaceLabel = workspaceRootPath
    ? (workspaceRootPath.split("/").filter(Boolean).pop() ?? "工作区")
    : "工作区";
  const header = { title: views.active.title, sub: views.active.subtitle };

  return (
    <aside
      aria-labelledby={titleId}
      aria-modal={isDrawerModal ? true : undefined}
      className={`workspace-panel${drawerOpen ? " is-drawer-open" : ""}`}
      ref={drawerDialogRef}
      role={isDrawerModal ? "dialog" : undefined}
      tabIndex={isDrawerModal ? -1 : undefined}
    >
      <header className="workspace-header">
        <div className="workspace-header-titles">
          <h2 id={titleId}>{header.title}</h2>
          <span className="workspace-header-sub">{header.sub}</span>
        </div>
        {pendingCount > 0 && tab === "artifacts" ? (
          <span className="workspace-badge">{pendingCount} 项待确认</span>
        ) : null}
        <button
          className="workspace-collapse"
          onClick={onCollapse}
          ref={closeButtonRef}
          type="button"
        >
          收起
        </button>
      </header>
      <WorkspaceViewTabs
        activeKind={views.activeKind}
        onActivate={views.activate}
        onClose={views.close}
        onCreate={views.open}
        views={views.views}
      />
      {tab === "browser" ? (
        <BrowserPlaceholder
          onOpenExternal={
            workspaceRootPath
              ? () => {
                  void chatApi
                    .revealInFinder(`${workspaceRootPath}/README.md`)
                    .catch(() => undefined);
                }
              : undefined
          }
        />
      ) : tab === "terminals" ? (
        <TerminalWorkspace
          activeId={terminals.activeId}
          busy={terminals.busy}
          error={terminals.error}
          onCloseSession={terminals.close}
          onCreate={terminals.create}
          onSelect={terminals.select}
          sessions={terminals.sessions}
          workspaceId={workspaceId}
        />
      ) : tab === "files" ? (
        <WorkspaceFilesPane
          conversationId={conversationId}
          focusPath={views.fileTarget?.path ?? null}
          focusNonce={views.fileTarget?.nonce ?? 0}
          rootPath={workspaceRootPath}
          workspaceId={workspaceId}
          workspaceName={workspaceLabel}
        />
      ) : selectedArtifactId ? (
        <ArtifactDetail
          artifactId={selectedArtifactId}
          conversationId={conversationId}
          latestTurnId={latestTurnId}
          onBack={() => setSelectedArtifactId(null)}
          onChanged={onWorkspaceRefresh}
          onOpenInFiles={(path) => views.openFile(path)}
          workspaceRootPath={workspaceRootPath}
        />
      ) : (
        <ul className="workspace-list">
          {workspace.artifacts.map((artifact) => (
            <li key={artifact.id}>
              <button onClick={() => setSelectedArtifactId(artifact.id)} type="button">
                <span className="workspace-item-title">{artifact.title}</span>
                <span className="workspace-item-meta">
                  <span className="proposal-kind">{kindLabel[artifact.kind]}</span>
                  v{artifact.currentVersionOrdinal} ·{" "}
                  {formatRelativeTime(artifact.updatedAt)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
