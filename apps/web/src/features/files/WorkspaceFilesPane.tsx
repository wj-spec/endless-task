import { useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import { ResizeHandle } from "../ui/ResizeHandle";
import { WorkspaceFileEditor } from "./FileEditor";
import { FileTree } from "./FileTree";
import { WorkspaceFileViewer } from "./FileViewer";
import { useElementWidth } from "./useElementWidth";
import { useWorkspaceTree } from "./useWorkspaceTree";

export type WorkspaceFilesPaneProps = {
  workspaceId: string;
  conversationId: string;
  rootPath?: string | null;
  workspaceName?: string;
};

/** 宽度阈值：≥ 此值用左右双栏，否则用主从切换（窄面板友好）。 */
export const WIDE_LAYOUT_MIN_WIDTH = 520;
const TREE_WIDTH_KEY = "endless-task-file-tree-width";
const TREE_WIDTH_DEFAULT = 240;
const TREE_WIDTH_MIN = 180;
const TREE_WIDTH_MAX = 360;

const readTreeWidth = (): number => {
  if (typeof window === "undefined") return TREE_WIDTH_DEFAULT;
  const stored = Number.parseFloat(localStorage.getItem(TREE_WIDTH_KEY) ?? "");
  if (!Number.isFinite(stored)) return TREE_WIDTH_DEFAULT;
  return Math.min(TREE_WIDTH_MAX, Math.max(TREE_WIDTH_MIN, stored));
};

/**
 * S11 文件面板：宽度感知布局。
 *
 * - 面板 ≥ 520px：左侧目录树（可拖拽 180–360px）+ 右侧预览，选中文件时树不消失；
 * - 面板 < 520px：主从切换（树 → 预览，带返回）。
 *
 * 根层加载失败（未绑定目录、目录不可用）在树上以错误行呈现，不阻塞页签切换。
 */
export function WorkspaceFilesPane({
  workspaceId,
  conversationId,
  rootPath,
  workspaceName,
}: WorkspaceFilesPaneProps) {
  const [showHidden, setShowHidden] = useState(false);
  const [hideNoisy, setHideNoisy] = useState(true);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [treeWidth, setTreeWidth] = useState(readTreeWidth);
  const [terminalNotice, setTerminalNotice] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);
  const [paneRef, paneWidth] = useElementWidth<HTMLDivElement>();
  const tree = useWorkspaceTree(workspaceId, showHidden);
  const wide = paneWidth >= WIDE_LAYOUT_MIN_WIDTH;

  useEffect(() => {
    setEditing(false);
  }, [selectedPath]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    localStorage.setItem(TREE_WIDTH_KEY, String(treeWidth));
  }, [treeWidth]);

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

  const treeView = (
    <FileTree
      children={tree.children}
      errors={tree.errors}
      expanded={tree.expanded}
      hideNoisy={hideNoisy}
      loading={tree.loading}
      onOpen={setSelectedPath}
      onToggle={tree.toggle}
      selectedPath={selectedPath}
      truncated={tree.truncated}
    />
  );

  const toolbar = (
    <div className="workspace-files-toolbar">
      <button
        aria-pressed={hideNoisy}
        className={hideNoisy ? "is-active" : undefined}
        onClick={() => setHideNoisy((value) => !value)}
        type="button"
      >
        {hideNoisy ? "显示降噪目录" : "隐藏降噪目录"}
      </button>
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
  );

  const notice = terminalNotice ? (
    <p className="file-tree-note" role="status">
      {terminalNotice}
    </p>
  ) : null;

  // 编辑态在两种布局下都占满预览区（宽面板时左侧树保持可见）。
  const previewContent = selectedPath ? (
    editing ? (
      <WorkspaceFileEditor
        conversationId={conversationId}
        onBack={() => {
          setEditing(false);
          if (!wide) setSelectedPath(null);
        }}
        onSaved={() => tree.refresh()}
        path={selectedPath}
        workspaceId={workspaceId}
      />
    ) : (
      <WorkspaceFileViewer
        onBack={
          wide
            ? undefined
            : () => {
                setEditing(false);
                setSelectedPath(null);
              }
        }
        onEdit={() => setEditing(true)}
        path={selectedPath}
        rootPath={rootPath}
        workspaceId={workspaceId}
        workspaceName={workspaceName}
      />
    )
  ) : null;

  if (!wide) {
    if (previewContent) {
      return (
        <div className="workspace-files" ref={paneRef}>
          {previewContent}
        </div>
      );
    }
    return (
      <div className="workspace-files" ref={paneRef}>
        {toolbar}
        {notice}
        {tree.rootBusy ? <p className="file-tree-note">正在读取…</p> : null}
        <div className="workspace-files-tree">{treeView}</div>
      </div>
    );
  }

  return (
    <div className="workspace-files" ref={paneRef}>
      {toolbar}
      {notice}
      <div
        className="workspace-files-body is-wide"
        style={{
          gridTemplateColumns: `${treeWidth}px 6px minmax(0, 1fr)`,
        }}
      >
        <div className="workspace-files-tree">
          {tree.rootBusy ? <p className="file-tree-note">正在读取…</p> : null}
          {treeView}
        </div>
        <ResizeHandle
          label="调整文件树宽度"
          max={TREE_WIDTH_MAX}
          min={TREE_WIDTH_MIN}
          onChange={setTreeWidth}
          onReset={() => setTreeWidth(TREE_WIDTH_DEFAULT)}
          value={treeWidth}
        />
        <div className="workspace-files-preview">
          {previewContent ?? (
            <p className="file-tree-note">选择左侧文件即可预览。</p>
          )}
        </div>
      </div>
    </div>
  );
}
