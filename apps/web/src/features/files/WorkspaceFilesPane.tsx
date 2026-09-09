import { useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import { WorkspaceFileEditor } from "./FileEditor";
import { FileTree } from "./FileTree";
import { WorkspaceFileViewer } from "./FileViewer";
import { useWorkspaceTree } from "./useWorkspaceTree";

export type WorkspaceFilesPaneProps = {
  workspaceId: string;
  conversationId: string;
  rootPath?: string | null;
};

/**
 * P0/P1 文件面板：目录树 + 只读预览 + 编辑保存（主从切换，窄面板友好）。
 *
 * 根层加载失败（未绑定目录、目录不可用）在树上以错误行呈现，不阻塞页签切换。
 */
export function WorkspaceFilesPane({
  workspaceId,
  conversationId,
  rootPath,
}: WorkspaceFilesPaneProps) {
  const [showHidden, setShowHidden] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [terminalNotice, setTerminalNotice] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);
  const tree = useWorkspaceTree(workspaceId, showHidden);

  useEffect(() => {
    setEditing(false);
  }, [selectedPath]);

  const openTerminal = async () => {
    setOpeningTerminal(true);
    setTerminalNotice(null);
    try {
      const result = await chatApi.openWorkspaceTerminal(workspaceId);
      setTerminalNotice(
        result.opened
          ? `已在系统终端打开工作区目录（${result.launcher}）。该终端内的命令不受应用逐条确认保护。`
          : result.message || "无法打开系统终端。",
      );
    } catch (cause: unknown) {
      setTerminalNotice(readableError(cause) || "无法打开系统终端。");
    } finally {
      setOpeningTerminal(false);
    }
  };

  if (!workspaceId) {
    return <p className="file-tree-note">该会话未绑定工作区，无法浏览文件。</p>;
  }

  if (selectedPath && editing) {
    return (
      <WorkspaceFileEditor
        conversationId={conversationId}
        onBack={() => {
          setEditing(false);
          setSelectedPath(null);
        }}
        onSaved={() => tree.refresh()}
        path={selectedPath}
        workspaceId={workspaceId}
      />
    );
  }

  if (selectedPath) {
    return (
      <WorkspaceFileViewer
        onBack={() => setSelectedPath(null)}
        onEdit={() => setEditing(true)}
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
        <button
          disabled={openingTerminal}
          onClick={() => void openTerminal()}
          type="button"
        >
          {openingTerminal ? "正在打开…" : "终端"}
        </button>
      </div>
      {terminalNotice ? (
        <p className="file-tree-note" role="status">
          {terminalNotice}
        </p>
      ) : null}
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
