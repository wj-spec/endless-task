import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { BrowseItem, EffectLogEntry, Workspace } from "../chat/apiTypes";
import { chatApi } from "../chat/api";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { useModalDialog } from "../ui/useModalDialog";
import { CloseIcon, FileIcon, FolderIcon } from "../ui/Icons";

type WorkspaceSettingsModalProps = {
  workspace: Workspace | null;
  onClose: () => void;
  onWorkspaceUpdated: (workspace: Workspace) => void;
  onDeleteWorkspace: (workspaceId: string) => Promise<void>;
};

const QUICK_LOCATIONS = ["~/Desktop", "~/Documents"];

const workspaceErrorText = (error: unknown): string => {
  if (typeof error === "object" && error !== null) {
    const maybe = error as { code?: string; message?: string };
    if (maybe.code === "workspace_not_empty") {
      return "当前工作区仍有会话，请先迁移这些会话后再删除工作区。";
    }
    if (maybe.code === "workspace_not_found") {
      return "该工作区已不存在。";
    }
    if (maybe.message) return maybe.message;
  }
  return "删除工作区失败，请重试。";
};

export function WorkspaceSettingsModal({
  workspace,
  onClose,
  onWorkspaceUpdated,
  onDeleteWorkspace,
}: WorkspaceSettingsModalProps) {
  const [currentPath, setCurrentPath] = useState<string | null>(null);
  const [items, setItems] = useState<BrowseItem[]>([]);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [binding, setBinding] = useState(false);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [logEntries, setLogEntries] = useState<EffectLogEntry[]>([]);
  const [showHidden, setShowHidden] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useModalDialog({
    initialFocusRef: closeButtonRef,
    onClose,
  });

  const loadDirectory = useCallback(async (path?: string) => {
    setLoading(true);
    setBrowseError(null);
    try {
      const result = await chatApi.browseFilesystem(path, showHidden);
      setCurrentPath(result.currentPath);
      setItems(result.items);
    } catch (error) {
      setBrowseError(error instanceof Error ? error.message : "无法浏览目录");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [showHidden]);

  useEffect(() => {
    void loadDirectory(undefined);
  }, [loadDirectory]);

  useEffect(() => {
    if (workspace?.rootPath) {
      void chatApi
        .shellLog(workspace.id, 50)
        .then(setLogEntries)
        .catch(() => setLogEntries([]));
    } else {
      setLogEntries([]);
    }
  }, [workspace?.id, workspace?.rootPath]);

  const parentPath = useMemo(() => {
    if (!currentPath) return null;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    return parts.length ? `/${parts.join("/")}` : "/";
  }, [currentPath]);

  const bindRoot = async (path: string) => {
    if (!workspace) return;
    setBinding(true);
    setBindingError(null);
    try {
      const updated = await chatApi.patchWorkspace(workspace.id, path);
      onWorkspaceUpdated(updated);
    } catch (error) {
      setBindingError(error instanceof Error ? error.message : "绑定失败");
    } finally {
      setBinding(false);
    }
  };

  const unbindRoot = async () => {
    if (!workspace) return;
    setBinding(true);
    setBindingError(null);
    try {
      const updated = await chatApi.patchWorkspace(workspace.id, null);
      onWorkspaceUpdated(updated);
      setLogEntries([]);
    } catch (error) {
      setBindingError(error instanceof Error ? error.message : "解绑失败");
    } finally {
      setBinding(false);
    }
  };

  return (
    <div
      className="overlay confirm-scrim workspace-settings-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        aria-labelledby={titleId}
        aria-modal="true"
        className="overlay-panel workspace-settings-panel"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="overlay-header">
          <h2 id={titleId}>工作区设置</h2>
          <button
            aria-label="关闭工作区设置"
            className="icon-button"
            onClick={onClose}
            ref={closeButtonRef}
            title="关闭"
            type="button"
          >
            <CloseIcon size={18} />
          </button>
        </header>
        <div className="overlay-body">
          <p className="settings-section-hint">
            {workspace
              ? `为「${workspace.name}」绑定本地目录后，助手才能在项目里读写文件、执行命令。`
              : "请先选择一个工作区。"}
          </p>

          {workspace?.rootPath ? (
            <div className="workspace-binding">
              <p className="workspace-binding-path">{workspace.rootPath}</p>
              <p className="settings-section-hint">runtime 已启用 · 文件系统与 shell 工具可用</p>
              <button
                className="danger-action"
                disabled={binding}
                onClick={() => void unbindRoot()}
                type="button"
              >
                解绑目录
              </button>
            </div>
          ) : (
            <div className="workspace-binding-empty">
              <p className="settings-section-hint">尚未绑定本地目录。</p>
              <button
                className="primary-action"
                disabled={binding || !workspace}
                onClick={() => void loadDirectory(undefined)}
                type="button"
              >
                选择目录…
              </button>
            </div>
          )}

          {bindingError && <p className="workspace-binding-error">{bindingError}</p>}

          {!workspace?.rootPath && (
            <div className="directory-picker">
              <div className="directory-picker-nav">
                {currentPath && <p className="directory-picker-path">{currentPath}</p>}
                <div className="directory-picker-actions">
                  <button
                    disabled={!parentPath}
                    onClick={() => void loadDirectory(parentPath ?? undefined)}
                    type="button"
                  >
                    上级
                  </button>
                  {QUICK_LOCATIONS.map((location) => (
                    <button
                      key={location}
                      onClick={() => void loadDirectory(location)}
                      type="button"
                    >
                      {location.split("/").pop()}
                    </button>
                  ))}
                  <label className="directory-hidden-toggle">
                    <input
                      checked={showHidden}
                      onChange={(event) => setShowHidden(event.target.checked)}
                      type="checkbox"
                    />
                    显示隐藏
                  </label>
                </div>
              </div>
              <div className="directory-picker-list">
                {loading && <p className="overlay-empty">正在加载…</p>}
                {browseError && <p className="workspace-binding-error">{browseError}</p>}
                {!loading &&
                  !browseError &&
                  items.map((item) => (
                    <div
                      className="directory-picker-row"
                      key={item.path}
                      role="button"
                      tabIndex={0}
                      onClick={() => {
                        if (item.kind === "directory") void loadDirectory(item.path);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && item.kind === "directory") {
                          void loadDirectory(item.path);
                        }
                      }}
                    >
                      <span
                        aria-hidden="true"
                        className={item.kind === "directory" ? "dir-icon" : "file-icon"}
                      >
                        {item.kind === "directory" ? (
                          <FolderIcon size={18} />
                        ) : (
                          <FileIcon size={18} />
                        )}
                      </span>
                      <span className="directory-picker-name">{item.name}</span>
                      {item.kind === "directory" && (
                        <button
                          className="directory-bind-button"
                          disabled={binding}
                          onClick={(event) => {
                            event.stopPropagation();
                            void bindRoot(item.path);
                          }}
                          type="button"
                        >
                          绑定到此目录
                        </button>
                      )}
                    </div>
                  ))}
              </div>
            </div>
          )}

          {workspace?.rootPath && logEntries.length > 0 && (
            <div className="shell-log">
              <h3 className="settings-section-title">命令历史</h3>
              <ul>
                {logEntries.map((entry, index) => (
                  <li className="shell-log-entry" key={`${entry.time}-${index}`}>
                    <span className="shell-log-operation">{entry.operation}</span>
                    <code>{entry.detail}</code>
                    <span className="shell-log-meta">
                      {entry.receipt.exitCode !== null &&
                        `退出码 ${entry.receipt.exitCode}`}
                      {entry.receipt.timedOut && " · 超时"}
                      {entry.receipt.unknownOutcome && " · 结果未知，请核对"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="workspace-section-divider" />
          <p className="settings-section-hint">
            删除工作区只移除注册与分组，不影响目录与已存文件；仍含会话的工作区不能删除。
          </p>
          {deleteError && <p className="workspace-binding-error">{deleteError}</p>}
          <button
            className="danger-action"
            disabled={deleting}
            onClick={() => {
              setDeleteError(null);
              setConfirmingDelete(true);
            }}
            type="button"
          >
            {deleting ? "删除中…" : "删除工作区"}
          </button>
          {confirmingDelete && workspace ? (
            <ConfirmDialog
              body="删除后该工作区将从列表移除，其会话与文件保留。含会话的工作区无法删除，请先迁移。"
              confirmLabel="删除工作区"
              onClose={() => setConfirmingDelete(false)}
              onConfirm={() => {
                setConfirmingDelete(false);
                setDeleting(true);
                setDeleteError(null);
                void onDeleteWorkspace(workspace.id)
                  .then(onClose)
                  .catch((error) => {
                    setDeleteError(workspaceErrorText(error));
                  })
                  .finally(() => setDeleting(false));
              }}
              title="删除这个工作区？"
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
