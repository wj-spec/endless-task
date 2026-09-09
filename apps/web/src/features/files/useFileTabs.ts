import { useCallback, useEffect, useMemo, useState } from "react";
import {
  closeOtherTabs,
  closeTab,
  DEFAULT_TABS_STATE,
  directoryTabOf,
  fileTabOf,
  openTab,
  parseFileTabs,
  serializeFileTabs,
  type FileTab,
  type FileTabsState,
} from "./fileTabs";

const STORAGE_PREFIX = "endless-task-file-tabs:";

export type FileTabsController = {
  tabs: FileTab[];
  active: FileTab;
  openDirectory: (path: string) => void;
  openFile: (path: string) => void;
  close: (id: string) => void;
  closeOthers: (id: string) => void;
  activate: (id: string) => void;
  /** 手动刷新：重新拉取当前目录（透传给目录内容）。 */
  refreshNonce: number;
  refresh: () => void;
};

const readStored = (workspaceId: string): FileTabsState => {
  if (typeof window === "undefined" || !workspaceId) return DEFAULT_TABS_STATE;
  return parseFileTabs(localStorage.getItem(`${STORAGE_PREFIX}${workspaceId}`));
};

/** S14：按工作区记忆的目录/文件标签页。 */
export function useFileTabs(workspaceId: string): FileTabsController {
  const [state, setState] = useState<FileTabsState>(() => readStored(workspaceId));
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    setState(readStored(workspaceId));
  }, [workspaceId]);

  useEffect(() => {
    if (typeof window === "undefined" || !workspaceId) return;
    localStorage.setItem(
      `${STORAGE_PREFIX}${workspaceId}`,
      serializeFileTabs(state),
    );
  }, [state, workspaceId]);

  const active = useMemo(
    () => state.tabs.find((tab) => tab.id === state.activeId) ?? state.tabs[0],
    [state],
  );

  const openDirectory = useCallback((path: string) => {
    setState((current) => openTab(current, directoryTabOf(path)));
  }, []);
  const openFile = useCallback((path: string) => {
    setState((current) => openTab(current, fileTabOf(path)));
  }, []);
  const close = useCallback((id: string) => {
    setState((current) => closeTab(current, id));
  }, []);
  const closeOthers = useCallback((id: string) => {
    setState((current) => closeOtherTabs(current, id));
  }, []);
  const activate = useCallback((id: string) => {
    setState((current) => ({ ...current, activeId: id }));
  }, []);

  return {
    tabs: state.tabs,
    active,
    openDirectory,
    openFile,
    close,
    closeOthers,
    activate,
    refreshNonce,
    refresh: () => setRefreshNonce((value) => value + 1),
  };
}
