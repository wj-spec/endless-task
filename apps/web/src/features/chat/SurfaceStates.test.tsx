// @vitest-environment jsdom
/**
 * `SurfaceStates`：空会话引导与加载态（从 `ChatWorkSurface.tsx` 底部搬出）。
 *
 * 三条引导语各自送出什么文案此前没有直测（只能靠 e2e 顺带碰），这里钉住——
 * 它们是新用户第一眼看到的交互。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EmptyConversation, LoadingState } from "./SurfaceStates";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
});

describe("EmptyConversation", () => {
  it("给出标题、说明与三条引导语", () => {
    act(() => {
      root.render(<EmptyConversation onSuggestion={() => {}} />);
    });
    const text = container.textContent ?? "";
    expect(text).toContain("今天想聊些什么？");
    expect(text).toContain("从一个问题、一个想法，或一件想理清的事开始。");
    expect(
      Array.from(container.querySelectorAll("button")).map((b) => b.textContent),
    ).toEqual([
      "帮我梳理今天最重要的事",
      "和我一起推敲一个想法",
      "把内容整理成一篇可复用文档",
    ]);
  });

  it("三条引导语各自点进输入框的文案正确", () => {
    const onSuggestion = vi.fn();
    act(() => {
      root.render(<EmptyConversation onSuggestion={onSuggestion} />);
    });
    const buttons = Array.from(container.querySelectorAll("button"));
    buttons.forEach((button) => {
      act(() => button.click());
    });
    expect(onSuggestion.mock.calls.map(([value]) => value)).toEqual([
      "帮我梳理一下今天最重要的三件事",
      "我有一个新想法，想和你一起推敲",
      "帮我把这段内容整理成一篇可复用的文档",
    ]);
  });

  it("提示里说明可以从回答处分叉保留上下文", () => {
    act(() => {
      root.render(<EmptyConversation onSuggestion={() => {}} />);
    });
    expect(container.querySelector(".empty-hint")?.textContent).toContain(
      "从此处分叉",
    );
  });
});

describe("LoadingState", () => {
  it("渲染带无障碍名的加载态（三个跳动点）", () => {
    act(() => {
      root.render(<LoadingState />);
    });
    const node = container.querySelector(".loading-state")!;
    expect(node.getAttribute("aria-label")).toBe("正在加载对话");
    expect(node.querySelectorAll("span")).toHaveLength(3);
  });
});
