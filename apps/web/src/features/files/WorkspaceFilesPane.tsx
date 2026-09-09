import { useState } from "react";
import { FileTree } from "./FileTree";
import { WorkspaceFileViewer } from "./FileViewer";
import { useWorkspaceTree } from "./useWorkspaceTree";

export type WorkspaceFilesPaneProps = {
  workspaceId: string;
  rootPath?: string | null;
};

/**
 * P0 文件面板：目录树 + 单文件只读预览（主从切换，窄面板友好）。
 *
 * 根层加载失败（未绑定目录、目录不可用）在树上以错误行呈现，不阻塞页签切换。
 */
export function WorkspaceFilesPane({
  workspaceId,
  rootPath,
}: WorkspaceFilesPaneProps) {
  const [showHidden, setShowHidden] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const tree = useWorkspaceTree(workspaceId, showHidden);

  if (!workspaceId) {
    return <p className="file-tree-note">该会话未绑定工作区，无法浏览文件。</p>;
  }

  if (selectedPath) {
    return (
      <WorkspaceFileViewer
        onBack={() => setSelectedPath(null)}
        path={selectedPath}
        rootPath={rootPath}
        workspaceId={workspaceId}
      />
    );
  }

  return (
    <div className="workspace-files">
      <div className="workspace-files-toolbar">
        <button
          aria-pressed={showHidden}
          className={showHidden ? "is-active" : undefined}
          onClick={() => setShowHidden((value) => !value)}
          type="button"
        >
          {showHidden ? "隐藏点文件" : "显示隐藏文件"}
        </button>
        <button onClick={tree.refresh} type="button">
          刷新
        </button>
      </div>
      {tree.rootBusy ? <p className="file-tree-note">正在读取…</p> : null}
      <div className="workspace-files-tree">
        <FileTree
          children={tree.children}
          errors={tree.errors}
          expanded={tree.expanded}
          loading={tree.loading}
          onOpen={setSelectedPath}
          onToggle={tree.toggle}
          truncated={tree.truncated}
        />
      </div>
    </div>
  );
}
