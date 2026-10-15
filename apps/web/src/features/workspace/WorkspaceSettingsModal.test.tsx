// @vitest-environment jsdom
/**
 * `WorkspaceSettingsModal`：工作区设置弹窗（P0-B 词级收口新增的最小覆盖）。
 *
 * 这个文件此前没有用例。这里**只**钉住本次改动的那段用户可见文案：
 * 「助手才能在**项目**里读写文件」已统一成「**工作区**」（同一个弹窗自己别处就写
 * "工作区"）。
 *
 * 弹窗内部会调 `chatApi`（浏览目录、生成终端等），这里统一桩掉，避免测试打真请求；
 * 用例只覆盖"没有工作区 / 新建工作区"这条**不触发任何请求**的初始分支。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Workspace } from "../chat/apiTypes";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { browseFilesystem } = vi.hoisted(() => ({
  browseFilesystem: vi.fn(async () => ({ currentPath: "/", items: [] })),
}));
vi.mock("../chat/api", () => ({
  ApiClientError: class extends Error {},
  chatApi: new Proxy(
    { browseFilesystem },
    {
      get: (target: Record<string, unknown>, prop: string) =>
        target[prop] ?? (() => Promise.resolve({ items: [] })),
    },
  ),
}));

const workspaceFixture: Workspace = {
  id: "ws_1",
  name: "个人工作区",
  rootPath: "/tmp/ws_1",
  createdAt: "2026-09-14T00:00:00.000Z",
  updatedAt: "2026-09-14T00:00:00.000Z",
};

const baseProps = {
  workspace: null as Workspace | null,
  onClose: () => {},
  onWorkspaceUpdated: () => {},
  onCreateWorkspace: async () => {
    throw new Error("本用例不应创建工作区");
  },
  onDeleteWorkspace: async () => {},
};

let container: HTMLDivElement;
let root: Root;

const render = async (
  props: Partial<typeof baseProps & { createMode: boolean }> = {},
) => {
  // 弹窗自身有两个 `useEffect`（列目录 / 取 shell 日志），它们会在 `await` 之后
  // 落一次 state。同步渲染会留下 "not wrapped in act(...)" 噪声，这里在 `act` 里
  // 把这一轮微任务冲掉，让断言看到的是稳定后的界面。
  await act(async () => {
    root.render(<WorkspaceSettingsModal {...baseProps} {...props} />);
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  browseFilesystem.mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const text = () => container.textContent ?? "";

describe("WorkspaceSettingsModal（P0-B「项目 → 工作区」）", () => {
  it("绑定前的提示用「工作区」，不再出现「项目里读写」", async () => {
    // 有工作区但尚未绑定目录 → 走 `:246` 那条提示（`workspace` 为 null 时走的是
    // "请先选择一个工作区。"，碰不到这条文案）。
    await render({ workspace: { ...workspaceFixture, rootPath: null } });
    expect(text()).toContain(
      "绑定本地目录后，助手才能在工作区里读写文件、执行命令。",
    );
    expect(text()).not.toContain("项目");
  });

  it("新建工作区表单的目录输入占位符也用「工作区」（同一弹窗不混用术语）", async () => {
    await render({ createMode: true, workspace: null });
    // 正向断言钉住新词（标签 + 占位符）；
    // `innerHTML` 的负向断言覆盖 `textContent` 看不到的 `placeholder` 属性。
    expect(text()).toContain("选择「一个本地目录」作为工作区根后，助手才能在工作区里读写文件、执行命令。");
    expect(text()).not.toContain("项目");
    expect(container.innerHTML).not.toContain("个人项目");
  });

  it("绑定成功后才提示「助手能力已启用」，不把 runtime 这类技术词暴露给用户", async () => {
    await render({ workspace: workspaceFixture });
    // 正向断言是判别点（把文案改回「runtime 已启用…」会打红这一条）。
    expect(text()).toContain("助手能力已启用 · 文件系统与命令工具可用");
    // 负向断言改成挂在**段落元素本身**上：`textContent` 会被 `shell-log-*` 这类
    // 类名之外的内容干扰，直接在渲染出的 `<p>` 文本上查更贴切。
    const hint = Array.from(container.querySelectorAll("p")).find((item) =>
      item.textContent?.includes("助手能力已启用"),
    );
    expect(hint).not.toBeUndefined();
    expect(hint!.textContent).not.toContain("runtime");
    expect(hint!.textContent).not.toContain("shell");
  });
});
