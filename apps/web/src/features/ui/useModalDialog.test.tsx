// @vitest-environment jsdom
import { useRef } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useModalDialog } from "./useModalDialog";

/**
 * 回归：焦点陷阱必须忽略"留在 DOM 里但没渲染出来"的元素。
 *
 * 真实案例：窄屏下侧栏拖拽手柄是 `display: none`，但它仍是第一个
 * `[tabindex]` 元素。陷阱把它当成首元素后，Shift+Tab 从真正的首元素
 * （收起侧栏按钮）往前退会掉到 body —— 焦点逃出弹层。
 * （jsdom 没有 `checkVisibility`，这里正好覆盖回退路径。）
 */
function TrapDialog() {
  const initialRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useModalDialog<HTMLDivElement>({
    initialFocusRef: initialRef,
    onClose: () => undefined,
  });
  return (
    <div aria-label="弹层" ref={dialogRef} role="dialog" tabIndex={-1}>
      <div style={{ display: "none" }} tabIndex={0}>
        隐藏的手柄
      </div>
      <button ref={initialRef} type="button">
        关掉
      </button>
      <button type="button">中间</button>
      <button type="button">最后一个</button>
    </div>
  );
}

let container: HTMLDivElement;
let root: Root;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const buttonByText = (text: string) =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
    (node) => node.textContent === text,
  )!;

const pressTab = (shiftKey = false) =>
  act(() => {
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true }),
    );
  });

describe("useModalDialog 焦点陷阱", () => {
  it("隐藏元素不进焦点序列：Shift+Tab 回绕到最后一个可见元素", () => {
    act(() => root.render(<TrapDialog />));
    expect(document.activeElement).toBe(buttonByText("关掉"));

    pressTab(true);
    expect(document.activeElement).toBe(buttonByText("最后一个"));
  });

  it("Tab 从最后一个可见元素回绕到第一个可见元素", () => {
    act(() => root.render(<TrapDialog />));
    buttonByText("最后一个").focus();

    pressTab(false);
    expect(document.activeElement).toBe(buttonByText("关掉"));
  });
});
