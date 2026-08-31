import { useEffect, useId, useRef, useState } from "react";
import type { WorkspaceSnapshot } from "../chat/apiTypes";
import { ArtifactDetail } from "./ArtifactDetail";
import { formatRelativeTime } from "./time";
import { useMediaQuery } from "../ui/useMediaQuery";
import { useModalDialog } from "../ui/useModalDialog";

type WorkspacePanelProps = {
  workspace: WorkspaceSnapshot;
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
  conversationId,
  latestTurnId,
  drawerOpen,
  onCollapse,
  onWorkspaceRefresh,
  workspaceRootPath,
}: WorkspacePanelProps) {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
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
          <h2 id={titleId}>工作区资产</h2>
          <span className="workspace-header-sub">此会话生成的产物与文档</span>
        </div>
        {pendingCount > 0 ? (
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
      {selectedArtifactId ? (
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
