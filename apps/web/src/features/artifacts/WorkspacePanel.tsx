import { useEffect, useState } from "react";
import type { WorkspaceSnapshot } from "../chat/apiTypes";
import { ArtifactDetail } from "./ArtifactDetail";
import { formatRelativeTime } from "./time";

type WorkspacePanelProps = {
  workspace: WorkspaceSnapshot;
  conversationId: string;
  latestTurnId: string | null;
  drawerOpen: boolean;
  onCollapse: () => void;
  onWorkspaceRefresh: () => void;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

export function WorkspacePanel({
  workspace,
  conversationId,
  latestTurnId,
  drawerOpen,
  onCollapse,
  onWorkspaceRefresh,
}: WorkspacePanelProps) {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);

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
      className={`workspace-panel${drawerOpen ? " is-drawer-open" : ""}`}
      aria-label="工作区"
    >
      <header className="workspace-header">
        <h2>工作区</h2>
        {pendingCount > 0 ? (
          <span className="workspace-badge">{pendingCount} 项待确认</span>
        ) : null}
        <button className="workspace-collapse" onClick={onCollapse} type="button">
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
