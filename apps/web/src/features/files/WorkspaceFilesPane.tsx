import { useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import { CloseIcon } from "../ui/Icons";
import { WorkspaceFileEditor } from "./FileEditor";
import { FileList } from "./FileList";
import { FileTypeIcon, categoryOfPath } from "./FileTypeIcon";
import { WorkspaceFileViewer } from "./FileViewer";
import { ROOT_TAB, breadcrumbOf, tabLabel } from "./fileTabs";
import { useFileTabs } from "./useFileTabs";
import { useWorkspaceTree } from "./useWorkspaceTree";

export type WorkspaceFilesPaneProps = {
  workspaceId: string;
  conversationId: string;
  rootPath?: string | null;
  workspaceName?: string;
  /** 交叉跳转：打开文件视图时定位到的路径（产物 → 文件）。 */
  focusPath?: string | null;
  /** 同路径再次跳转时的触发序号。 */
  focusNonce?: number;
};

/**
 * S14 文件视图：**目录/文件标签页**。
 *
 * - 目录标签：同一级平铺列出该目录直接子项，图标按分类；
 * - 文件标签：预览 / 编辑独占面板（Markdown 仍限可读宽度）；
 * - 标签按工作区记忆；根目录标签常驻、最后一个标签不可关闭。
 */
export function WorkspaceFilesPane({
  workspaceId,
  conversationId,
  rootPath,
  workspaceName,
  focusPath,
  focusNonce,
}: WorkspaceFilesPaneProps) {
  const [showHidden, setShowHidden] = useState(false);
  const [hideNoisy, setHideNoisy] = useState(true);
  const [editing, setEditing] = useState(false);
  const [terminalNotice, setTerminalNotice] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);
  const tree = useWorkspaceTree(workspaceId, showHidden);
  const tabs = useFileTabs(workspaceId);
  const workspaceLabel = workspaceName || "工作区";

  // 目录标签打开时预取该层内容（含手动刷新）。
  useEffect(() => {
    if (tabs.active.kind !== "directory") return;
    tree.ensure(tabs.active.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.active.kind, tabs.active.path, tabs.refreshNonce]);

  useEffect(() => {
    setEditing(false);
  }, [tabs.active.id]);

  // 交叉跳转：产物 →「在文件中打开」时打开/聚焦该文件标签。
  useEffect(() => {
    if (!focusPath) return;
    setEditing(false);
    tabs.openFile(focusPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusPath, focusNonce]);

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

  const activePath = tabs.active.path;
  const directoryEntries = tree.children[activePath] ?? [];
  const directoryBusy =
    tree.loading.includes(activePath) && !(activePath in tree.children);

  return (
    <div className="workspace-files">
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
        <button onClick={tabs.refresh} type="button">
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
      <div aria-label="已打开的文件标签" className="file-tabs" role="tablist">
        {tabs.tabs.map((tab) => (
          <span
            className={tab.id === tabs.active.id ? "file-tab is-active" : "file-tab"}
            key={tab.id}
          >
            <button
              aria-selected={tab.id === tabs.active.id}
              className="file-tab-label"
              onClick={() => tabs.activate(tab.id)}
              role="tab"
              title={tab.path || workspaceLabel}
              type="button"
            >
              <FileTypeIcon
                category={categoryOfPath(tab.path, tab.kind)}
                size={13}
              />
              <span className="file-tab-name">{tabLabel(tab, workspaceLabel)}</span>
            </button>
            {tab.id === ROOT_TAB.id ? null : (
              <button
                aria-label={`关闭 ${tabLabel(tab, workspaceLabel)}`}
                className="file-tab-close"
                onClick={() => tabs.close(tab.id)}
                type="button"
              >
                <CloseIcon size={11} />
              </button>
            )}
          </span>
        ))}
      </div>
      <div className="file-tab-body">
        {tabs.active.kind === "directory" ? (
          <>
            <nav aria-label="目录路径" className="file-crumbs">
              {breadcrumbOf(activePath, workspaceLabel).map((crumb, index, all) => (
                <span className="file-crumb" key={crumb.path || "root"}>
                  {index === all.length - 1 ? (
                    <span className="file-crumb-current">{crumb.label}</span>
                  ) : (
                    <button
                      className="file-crumb-button"
                      onClick={() => tabs.openDirectory(crumb.path)}
                      type="button"
                    >
                      {crumb.label}
                    </button>
                  )}
                </span>
              ))}
            </nav>
            <div className="file-list-scroll">
              <FileList
                busy={directoryBusy}
                entries={directoryEntries}
                error={tree.errors[activePath] ?? null}
                hideNoisy={hideNoisy}
                onOpenDirectory={tabs.openDirectory}
                onOpenFile={tabs.openFile}
                truncated={tree.truncated[activePath] === true}
              />
            </div>
          </>
        ) : editing ? (
          <WorkspaceFileEditor
            conversationId={conversationId}
            onBack={() => {
              setEditing(false);
              tabs.close(tabs.active.id);
            }}
            onSaved={() => tree.refresh()}
            path={activePath}
            workspaceId={workspaceId}
          />
        ) : (
          <WorkspaceFileViewer
            onClose={() => tabs.close(tabs.active.id)}
            onEdit={() => setEditing(true)}
            onOpenDirectory={tabs.openDirectory}
            path={activePath}
            rootPath={rootPath}
            workspaceId={workspaceId}
            workspaceName={workspaceLabel}
          />
        )}
      </div>
    </div>
  );
}
