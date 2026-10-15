// @vitest-environment jsdom
/** N1③：「变更」视图的 dom 契约 —— diff 真的渲染出来、空态/错态如实。
 *
 * 只 spy 真 `chatApi`（不整模块 mock，批次 D 的教训）：断言渲染出来的可见文本。
 */

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatApi } from "../../chat/api";
import type { ConversationChangeItem } from "../../chat/apiTypes";
import { ChangesView } from "./ChangesView";

function render(element: ReactElement): { container: HTMLElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(element);
  });
  return { container, root };
}

const ITEM: ConversationChangeItem = {
  id: "entry_1",
  target: "notes.md",
  kind: "file_write",
  status: "available",
  undoable: true,
  createdAt: "2026-10-13T00:00:00.000Z",
  beforeAvailable: true,
  diff: "--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n-旧行\n+新行",
};

describe("ChangesView", () => {
  let mounted: Array<{ container: HTMLElement; root: Root }> = [];

  beforeEach(() => {
    mounted = [];
  });

  afterEach(() => {
    for (const item of mounted) {
      act(() => {
        item.root.unmount();
      });
      item.container.remove();
    }
    vi.restoreAllMocks();
  });

  async function settle() {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("渲染 unified diff 的可见文本（含 + / − 两侧）", async () => {
    vi.spyOn(chatApi, "listConversationChanges").mockResolvedValue({
      items: [ITEM],
    });
    const view = render(<ChangesView conversationId="conv_1" />);
    mounted.push(view);
    await settle();

    expect(view.container.textContent).toContain("notes.md");
    expect(view.container.textContent).toContain("-旧行");
    expect(view.container.textContent).toContain("+新行");
  });

  it("空清单如实显示空态", async () => {
    vi.spyOn(chatApi, "listConversationChanges").mockResolvedValue({
      items: [],
    });
    const view = render(<ChangesView conversationId="conv_1" />);
    mounted.push(view);
    await settle();

    expect(view.container.textContent).toContain("本会话还没有文件变更。");
  });

  it("加载失败如实显示错误，不假装空态", async () => {
    vi.spyOn(chatApi, "listConversationChanges").mockRejectedValue(
      new Error("boom"),
    );
    const view = render(<ChangesView conversationId="conv_1" />);
    mounted.push(view);
    await settle();

    expect(view.container.textContent).toContain("变更清单加载失败");
    expect(view.container.textContent).not.toContain("本会话还没有文件变更。");
  });

  it("diff 为 null 时显示无法对比的如实说明", async () => {
    vi.spyOn(chatApi, "listConversationChanges").mockResolvedValue({
      items: [{ ...ITEM, diff: null, beforeAvailable: false }],
    });
    const view = render(<ChangesView conversationId="conv_1" />);
    mounted.push(view);
    await settle();

    expect(view.container.textContent).toContain("变更前内容不可用");
  });

  it("请求打到正确的变更端点（接线钉：URL 写错必须红）", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init });
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }),
    );
    try {
      await chatApi.listConversationChanges("conv_9");
    } finally {
      vi.unstubAllGlobals();
    }

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("/conversations/conv_9/changes");
    expect(calls[0].init?.method ?? "GET").toBe("GET");
  });
});
