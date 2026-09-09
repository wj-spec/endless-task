/** S14：文件视图的目录/文件标签页模型（纯函数，便于单测）。 */

export type FileTabKind = "directory" | "file";

export type FileTab = {
  id: string;
  kind: FileTabKind;
  /** 相对工作区根的路径；根目录为空串。 */
  path: string;
};

export type FileTabsState = {
  tabs: FileTab[];
  activeId: string;
};

export const ROOT_TAB: FileTab = { id: "directory:", kind: "directory", path: "" };

export const tabIdOf = (kind: FileTabKind, path: string): string =>
  `${kind}:${path}`;

export const fileTabOf = (path: string): FileTab => ({
  id: tabIdOf("file", path),
  kind: "file",
  path,
});

export const directoryTabOf = (path: string): FileTab => ({
  id: tabIdOf("directory", path),
  kind: "directory",
  path,
});

/** 标签显示名：根目录显示工作区名，其余取路径末段。 */
export function tabLabel(tab: FileTab, workspaceLabel = "工作区"): string {
  if (tab.path === "") return workspaceLabel;
  const parts = tab.path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? tab.path;
}

/** 打开标签：已存在则聚焦，不存在则追加到末尾并激活。 */
export function openTab(state: FileTabsState, tab: FileTab): FileTabsState {
  const exists = state.tabs.some((item) => item.id === tab.id);
  return {
    tabs: exists ? state.tabs : [...state.tabs, tab],
    activeId: tab.id,
  };
}

/** 关闭标签：根目录不可关；关掉激活项时激活其右邻（否则左邻）。 */
export function closeTab(state: FileTabsState, id: string): FileTabsState {
  if (id === ROOT_TAB.id) return state;
  const index = state.tabs.findIndex((item) => item.id === id);
  if (index < 0) return state;
  const tabs = state.tabs.filter((item) => item.id !== id);
  const nextTabs = tabs.length > 0 ? tabs : [ROOT_TAB];
  if (state.activeId !== id) return { tabs: nextTabs, activeId: state.activeId };
  const neighbour = nextTabs[Math.min(index, nextTabs.length - 1)] ?? ROOT_TAB;
  return { tabs: nextTabs, activeId: neighbour.id };
}

/** 关闭除某个标签外的全部（根目录标签保留）。 */
export function closeOtherTabs(state: FileTabsState, id: string): FileTabsState {
  const kept = state.tabs.filter((item) => item.id === id || item.id === ROOT_TAB.id);
  const tabs = kept.some((item) => item.id === id) ? kept : [...kept, ROOT_TAB];
  return { tabs: tabs.length > 0 ? tabs : [ROOT_TAB], activeId: id };
}

export const DEFAULT_TABS_STATE: FileTabsState = {
  tabs: [ROOT_TAB],
  activeId: ROOT_TAB.id,
};

/** 解析持久化状态；非法输入回落到根目录标签。 */
export function parseFileTabs(raw: string | null): FileTabsState {
  if (!raw) return DEFAULT_TABS_STATE;
  try {
    const parsed = JSON.parse(raw) as {
      tabs?: { kind?: string; path?: string }[];
      active?: string;
    };
    const tabs: FileTab[] = [];
    for (const item of parsed.tabs ?? []) {
      if (item?.kind !== "directory" && item?.kind !== "file") continue;
      const path = typeof item.path === "string" ? item.path : "";
      const tab =
        item.kind === "directory" ? directoryTabOf(path) : fileTabOf(path);
      if (!tabs.some((existing) => existing.id === tab.id)) tabs.push(tab);
    }
    if (!tabs.some((tab) => tab.id === ROOT_TAB.id)) tabs.unshift(ROOT_TAB);
    const activeId =
      typeof parsed.active === "string" &&
      tabs.some((tab) => tab.id === parsed.active)
        ? parsed.active
        : ROOT_TAB.id;
    return { tabs, activeId };
  } catch {
    return DEFAULT_TABS_STATE;
  }
}

export function serializeFileTabs(state: FileTabsState): string {
  return JSON.stringify({
    tabs: state.tabs.map((tab) => ({ kind: tab.kind, path: tab.path })),
    active: state.activeId,
  });
}

/** 目录标签的面包屑（根显示工作区名）。 */
export function breadcrumbOf(
  path: string,
  workspaceLabel = "工作区",
): { label: string; path: string }[] {
  const parts = path.split("/").filter(Boolean);
  const crumbs = [{ label: workspaceLabel, path: "" }];
  parts.forEach((label, index) => {
    crumbs.push({ label, path: parts.slice(0, index + 1).join("/") });
  });
  return crumbs;
}
