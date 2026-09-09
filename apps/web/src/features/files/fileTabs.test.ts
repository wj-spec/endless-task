import { describe, expect, it } from "vitest";
import {
  DEFAULT_TABS_STATE,
  ROOT_TAB,
  breadcrumbOf,
  closeOtherTabs,
  closeTab,
  directoryTabOf,
  fileTabOf,
  openTab,
  parseFileTabs,
  serializeFileTabs,
  tabLabel,
} from "./fileTabs";

describe("fileTabs（目录/文件标签模型）", () => {
  it("打开目录与文件：去重并聚焦", () => {
    let state = openTab(DEFAULT_TABS_STATE, directoryTabOf("src"));
    state = openTab(state, fileTabOf("src/App.tsx"));
    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "directory:",
      "directory:src",
      "file:src/App.tsx",
    ]);
    expect(state.activeId).toBe("file:src/App.tsx");

    // 再次打开同一目录只聚焦，不新增
    state = openTab(state, directoryTabOf("src"));
    expect(state.tabs).toHaveLength(3);
    expect(state.activeId).toBe("directory:src");
  });

  it("关闭激活标签时激活右邻，根目录不可关闭", () => {
    let state = openTab(DEFAULT_TABS_STATE, directoryTabOf("src"));
    state = openTab(state, fileTabOf("src/App.tsx"));
    state = openTab(state, directoryTabOf("docs"));

    state = closeTab(state, "file:src/App.tsx");
    expect(state.activeId).toBe("directory:docs");

    state = closeTab(state, ROOT_TAB.id);
    expect(state.tabs[0]).toEqual(ROOT_TAB);
  });

  it("全部关闭后保留根目录标签", () => {
    let state = openTab(DEFAULT_TABS_STATE, fileTabOf("a.txt"));
    state = closeTab(state, "file:a.txt");
    expect(state).toEqual(DEFAULT_TABS_STATE);
  });

  it("关闭其他标签时保留根目录", () => {
    let state = openTab(DEFAULT_TABS_STATE, directoryTabOf("src"));
    state = openTab(state, fileTabOf("src/App.tsx"));
    const closed = closeOtherTabs(state, "file:src/App.tsx");
    expect(closed.tabs.map((tab) => tab.id)).toEqual(["directory:", "file:src/App.tsx"]);
    expect(closed.activeId).toBe("file:src/App.tsx");
  });

  it("持久化往返一致，非法输入回落", () => {
    let state = openTab(DEFAULT_TABS_STATE, directoryTabOf("src"));
    state = openTab(state, fileTabOf("README.md"));
    expect(parseFileTabs(serializeFileTabs(state))).toEqual(state);

    expect(parseFileTabs(null)).toEqual(DEFAULT_TABS_STATE);
    expect(parseFileTabs("{bad json")).toEqual(DEFAULT_TABS_STATE);
    expect(parseFileTabs(JSON.stringify({ tabs: [{ kind: "weird", path: "x" }] }))).toEqual(
      DEFAULT_TABS_STATE,
    );
  });

  it("标签名与面包屑", () => {
    expect(tabLabel(ROOT_TAB, "myproj")).toBe("myproj");
    expect(tabLabel(directoryTabOf("src/components"))).toBe("components");
    expect(breadcrumbOf("src/components", "myproj")).toEqual([
      { label: "myproj", path: "" },
      { label: "src", path: "src" },
      { label: "components", path: "src/components" },
    ]);
    expect(breadcrumbOf("", "myproj")).toEqual([{ label: "myproj", path: "" }]);
  });
});
