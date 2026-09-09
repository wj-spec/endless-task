import { useEffect, useId, useRef, useState } from "react";
import type { WorkspaceSnapshot } from "../chat/apiTypes";
import { WorkspaceFilesPane } from "../files/WorkspaceFilesPane";
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

type WorkspacePanelTab = "artifacts" | "files";

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

const TAB_LABELS: Record<WorkspacePanelTab, string> = {
  artifacts: "资产",
  files: "文件",
};

const TAB_TITLES: Record<WorkspacePanelTab, { title: string; sub: string }> = {
  artifacts: { title: "工作区资产", sub: "此会话生成的产物与文档" },
  files: { title: "工作区文件", sub: "浏览与预览绑定的本地目录" },
};

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
  const [tab, setTab] = useState<WorkspacePanelTab>("artifacts");
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
  const header = TAB_TITLES[tab];

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
      <div aria-label="工作区面板" className="workspace-tabs" role="tablist">
        {(Object.keys(TAB_LABELS) as WorkspacePanelTab[]).map((key) => (
          <button
            aria-selected={tab === key}
            className={tab === key ? "is-active" : undefined}
            key={key}
            onClick={() => setTab(key)}
            role="tab"
            type="button"
          >
            {TAB_LABELS[key]}
          </button>
        ))}
      </div>
      {tab === "files" ? (
        <WorkspaceFilesPane
          conversationId={conversationId}
          rootPath={workspaceRootPath}
          workspaceId={workspaceId}
        />
      ) : selectedArtifactId ? (
        <ArtifactDetail
          artifactId={selectedArtifactId}
          conversationId={conversationId}
          latestTurnId={latestTurnId}
          onBack={() => setSelectedArtifactId(null)}
          onChanged={onWorkspaceRefresh}
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
