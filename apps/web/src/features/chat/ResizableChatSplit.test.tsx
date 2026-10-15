// @vitest-environment jsdom
/**
 * `ResizableChatSplit`：主/侧两栏的分隔条（P0-B 词级收口新增的最小覆盖）。
 *
 * 这个文件此前没有用例。这里**只**钉住本次改动的那条读屏文案——
 * 「主会话」已统一成「主线」。
 * 不覆盖拖拽/键盘调整宽度等既有行为（那是另一个任务的范围）。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ResizableChatSplit } from "./ResizableChatSplit";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  localStorage.clear();
});

describe("ResizableChatSplit（P0-B 词级收口）", () => {
  it("侧栏打开时，分隔条的读屏名称与读数都用「主线」（不再出现「主会话」）", () => {
    act(() => {
      root.render(
        <ResizableChatSplit sideOpen>
          <div data-testid="main">主线面</div>
          <div data-testid="side">对照面</div>
        </ResizableChatSplit>,
      );
    });

    const separator = container.querySelector('[role="separator"]')!;
    expect(separator.getAttribute("aria-label")).toBe("调整主线与分支宽度");
    expect(separator.getAttribute("aria-valuetext")).toBe("主线 60%，分支 40%");

    const html = container.innerHTML;
    expect(html).not.toContain("主会话");
    expect(html).not.toContain("调整主会话与分支宽度");
  });

  it("侧栏关闭时不渲染分隔条（原本就没有可调整的两栏）", () => {
    act(() => {
      root.render(
        <ResizableChatSplit sideOpen={false}>
          <div data-testid="main">主线面</div>
          <div data-testid="side">对照面</div>
        </ResizableChatSplit>,
      );
    });
    expect(container.querySelector('[role="separator"]')).toBeNull();
  });
});
