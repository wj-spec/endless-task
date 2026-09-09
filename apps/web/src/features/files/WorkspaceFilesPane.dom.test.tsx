// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";

const entry = (over: Partial<WorkspaceTreeEntry>): WorkspaceTreeEntry => ({
  name: "file",
  relativePath: "file",
  kind: "file",
  size: 10,
  isHidden: false,
  ...over,
});

const LISTINGS: Record<string, WorkspaceTreeEntry[]> = {
  "": [
    entry({ name: "src", relativePath: "src", kind: "directory" }),
    entry({ name: "README.md", relativePath: "README.md", size: 2048 }),
  ],
  src: [
    entry({ name: "App.tsx", relativePath: "src/App.tsx", size: 512 }),
    entry({ name: "hooks", relativePath: "src/hooks", kind: "directory" }),
  ],
};

vi.mock("../chat/api", () => ({
  chatApi: {
    listWorkspaceTree: vi.fn(async (_workspaceId: string, path: string) => ({
      entries: LISTINGS[path] ?? [],
      truncated: false,
    })),
    previewWorkspaceFile: vi.fn(async () => ({
      lines: [{ line: 1, text: "# hello" }],
      totalLines: 1,
      startLine: 1,
      endLine: 1,
      path: "README.md",
      truncated: false,
    })),
    openWorkspaceTerminal: vi.fn(async () => ({
      opened: true,
      cwd: "/tmp",
      launcher: "open -a Terminal",
      message: "",
    })),
  },
}));

const { WorkspaceFilesPane } = await import("./WorkspaceFilesPane");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

const tabs = () =>
  Array.from(container.querySelectorAll<HTMLButtonElement>(".file-tab-label")).map(
    (node) => node.textContent?.trim(),
  );

const listItems = () =>
  Array.from(container.querySelectorAll<HTMLLIElement>(".file-list-item")).map(
    (node) => node.querySelector(".file-list-name")?.textContent,
  );

beforeEach(() => {
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const renderPane = async (focusPath?: string) => {
  await act(async () => {
    root.render(
      <WorkspaceFilesPane
        conversationId="conv_1"
        focusPath={focusPath ?? null}
        focusNonce={focusPath ? 1 : 0}
        workspaceId="ws_1"
        workspaceName="myproj"
      />,
    );
  });
  await flush();
};

describe("WorkspaceFilesPane 标签页交互（S14）", () => {
  it("根目录标签平铺列出内容", async () => {
    await renderPane();
    expect(tabs()).toEqual(["myproj"]);
    expect(listItems()).toEqual(["src", "README.md"]);
    expect(container.textContent).toContain("2.0 KB");
  });

  it("点目录开新标签并切换内容，重复点只聚焦", async () => {
    await renderPane();
    const src = Array.from(
      container.querySelectorAll<HTMLLIElement>(".file-list-item"),
    ).find((node) => node.textContent?.includes("src"))!;
    await act(async () => src.click());
    await flush();

    expect(tabs()).toEqual(["myproj", "src"]);
    expect(listItems()).toEqual(["App.tsx", "hooks"]);
    expect(container.querySelector(".file-crumb-current")?.textContent).toBe("src");

    // 回根目录再点 src：不新增标签
    const rootTab = container.querySelector<HTMLButtonElement>(".file-tab-label")!;
    await act(async () => rootTab.click());
    await flush();
    const srcAgain = Array.from(
      container.querySelectorAll<HTMLLIElement>(".file-list-item"),
    ).find((node) => node.textContent?.includes("src"))!;
    await act(async () => srcAgain.click());
    await flush();
    expect(tabs()).toEqual(["myproj", "src"]);
  });

  it("点文件开文件标签并加载预览", async () => {
    await renderPane();
    const readme = Array.from(
      container.querySelectorAll<HTMLLIElement>(".file-list-item"),
    ).find((node) => node.textContent?.includes("README.md"))!;
    await act(async () => readme.click());
    await flush();

    expect(tabs()).toEqual(["myproj", "README.md"]);
    expect(container.querySelector(".file-viewer")).not.toBeNull();
    expect(container.textContent).toContain("hello");
  });

  it("交叉跳转：focusPath 直接打开文件标签（产物 → 文件）", async () => {
    await renderPane("README.md");
    expect(tabs()).toEqual(["myproj", "README.md"]);
    expect(container.querySelector(".file-viewer")).not.toBeNull();
  });

  it("关闭标签：文件标签可关，根目录标签不可关", async () => {
    await renderPane();
    const readme = Array.from(
      container.querySelectorAll<HTMLLIElement>(".file-list-item"),
    ).find((node) => node.textContent?.includes("README.md"))!;
    await act(async () => readme.click());
    await flush();

    const close = container.querySelector<HTMLButtonElement>(
      'button[aria-label="关闭 README.md"]',
    )!;
    await act(async () => close.click());
    await flush();

    expect(tabs()).toEqual(["myproj"]);
    expect(
      container.querySelector('button[aria-label="关闭 myproj"]'),
    ).toBeNull();
  });
});
