// @vitest-environment jsdom
/**
 * 批次 H / F-5：**标识「工作区」的 FolderIcon 必须只有一个尺寸事实源**。
 *
 * 控制器取证：同一个 `FolderIcon` 在**同一屏**上有三种尺寸 ——
 * 左栏工作区行 `18`（且 `rail.css` 又硬编码了一遍 `width/height:18px`）、
 * 聊天头工作区名旁的焦点标签 `15`、聊天头右侧工作区面板按钮 `19`。
 * 同一个「工作区」概念在 15/18/19 之间跳、尺寸还有两个来源。
 *
 * 本文件把「同一屏里标识工作区的 svg 尺寸相同」钉住：渲染真的 `WorkspaceNavigationGroup`
 * （左栏）+ 真的 `ChatSurfaceHeader`（聊天头），读两处 svg 的 `width`/`height` 属性。
 * 反向变异（把任一处改回 15/19）⇒ 必红。
 *
 * 注意 jsdom 不做布局，`getBoundingClientRect()` 恒为 0，所以**不能**用它会话里的
 * 真实盒模型来断言尺寸；这里断言的是 svg 的 `width`/`height` **属性**
 * （`WORKSPACE_ICON_SIZE` 直接写进属性，CSS 不再重复声明尺寸——这正是 F-5 的要求：
 * 单一事实源，CSS 只做布局）。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { WORKSPACE_ICON_SIZE } from "../ui/Icons";
import { ChatSurfaceHeader, type ChatSurfaceHeaderProps } from "./ChatSurfaceHeader";
import type { SessionActions } from "./workspaceNavigationModel";
import { WorkspaceNavigationGroup } from "./WorkspaceNavigationGroup";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const workspace = {
  id: "ws-1",
  name: "工作区一",
  rootPath: "/tmp/ws1",
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
};

const headerProps: ChatSurfaceHeaderProps = {
  activeWorkspace: workspace,
  archived: false,
  branchLanes: [],
  // 侧边按钮（`surface-header-side`）只在有会话时渲染；这里给最小快照让它出现。
  conversation: {
    conversation: {
      id: "conv-1",
      title: "会话一",
      status: "active",
      nextTurnOrdinal: 1,
      titleIsManual: false,
      createdAt: "2026-09-01T00:00:00Z",
      updatedAt: "2026-09-01T00:00:00Z",
      archivedAt: null,
      parentConversationId: null,
      forkTurnId: null,
      kind: "normal",
      providerProfileId: null,
      modelOverride: null,
      promotedAt: null,
      workspaceId: workspace.id,
    },
    turns: [],
    files: [],
  },
  currentLaneId: null,
  currentLaneLabel: "主线",
  isGenerating: false,
  pendingAction: null,
  renaming: false,
  sideMode: null,
  titleDraft: "",
  variant: "main",
  onArchive: () => {},
  onMenu: () => {},
  onRequestSideClose: () => {},
  onRestore: () => {},
  onSearch: () => {},
  onSetConfirmingDelete: () => {},
  onSetRenaming: () => {},
  onSubmitTitle: () => {},
  onTitleDraftChange: () => {},
  onToggleWorkspace: () => {},
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.querySelectorAll(".workspace-hover-info").forEach((node) => node.remove());
});

/** 同一屏：左栏工作区行 + 聊天头（工作区名标签 + 工作区面板按钮）。 */
const renderSameScreen = () => {
  act(() =>
    root.render(
      <>
        <WorkspaceNavigationGroup
          actions={{ onNewConversation: () => {} } as unknown as SessionActions}
          activeConversationId={null}
          currentWorkspaceId={workspace.id}
          group={{
            id: workspace.id,
            name: workspace.name,
            rootPath: workspace.rootPath,
            createdAt: workspace.createdAt,
            conversations: [],
            totalCount: 0,
          }}
          homePath="/tmp/home"
          onToggleOpen={() => {}}
          onToggleShowAll={() => {}}
          open
          renameDraft=""
          renamingId={null}
          showAll={false}
        />
        <ChatSurfaceHeader {...headerProps} />
      </>,
    ),
  );
};

const svgSizes = (node: Element | null) => {
  const svg = node?.querySelector("svg");
  if (!svg) return null;
  return {
    width: svg.getAttribute("width"),
    height: svg.getAttribute("height"),
  };
};

describe("F-5 工作区图标尺寸单一事实源", () => {
  it("同一屏里标识工作区的 svg 尺寸相同（左栏 / 工作区名标签 / 工作区面板按钮）", () => {
    renderSameScreen();

    const rail = container.querySelector(".workspace-folder-icon");
    const heading = container.querySelector(".heading-focus-icon");
    const panel = container.querySelector(
      'button[aria-label="打开工作区侧边栏"]',
    );
    expect(rail).not.toBeNull();
    expect(heading).not.toBeNull();
    expect(panel).not.toBeNull();

    const sizes = [svgSizes(rail), svgSizes(heading), svgSizes(panel)];
    expect(sizes).toEqual([
      { width: String(WORKSPACE_ICON_SIZE), height: String(WORKSPACE_ICON_SIZE) },
      { width: String(WORKSPACE_ICON_SIZE), height: String(WORKSPACE_ICON_SIZE) },
      { width: String(WORKSPACE_ICON_SIZE), height: String(WORKSPACE_ICON_SIZE) },
    ]);
    // 单一事实源：三处 svg 的属性值必须两两相等（不是"恰好都等于某个巧合数字"）。
    expect(sizes[0]).toEqual(sizes[1]);
    expect(sizes[1]).toEqual(sizes[2]);
  });
});
