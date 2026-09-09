// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { RowMenu } from "./RowMenu";

let container: HTMLDivElement;
let root: Root;

// React 19 的 act 环境标记，避免 jsdom 下打印 "not configured to support act"。
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.querySelectorAll(".row-menu-pop").forEach((node) => node.remove());
});

const openMenu = (ui: React.ReactElement) => {
  act(() => root.render(ui));
  const trigger = container.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]');
  expect(trigger).not.toBeNull();
  act(() => trigger!.click());
  return document.querySelector<HTMLDivElement>(".row-menu-pop");
};

describe("RowMenu 浮层（portal）", () => {
  it("菜单渲染到 body 而不是触发器内部——避免被 overflow 容器裁切", () => {
    // 模拟会话栏/标签条那样的裁剪容器
    const clip = document.createElement("div");
    clip.style.overflow = "hidden";
    document.body.appendChild(clip);
    const clipRoot = createRoot(clip);
    act(() =>
      clipRoot.render(
        <RowMenu items={[{ label: "文件", onSelect: () => undefined }]} />,
      ),
    );
    const trigger = clip.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]');
    act(() => trigger!.click());

    const menu = document.querySelector<HTMLDivElement>(".row-menu-pop");
    expect(menu).not.toBeNull();
    expect(clip.contains(menu)).toBe(false);
    expect(menu!.parentElement).toBe(document.body);
    expect(menu!.classList.contains("is-floating")).toBe(true);
    expect(menu!.style.position).not.toBe("absolute");

    act(() => clipRoot.unmount());
    clip.remove();
  });

  it("点击外部关闭，点击菜单项触发回调", () => {
    let selected = "";
    const menu = openMenu(
      <RowMenu
        items={[{ label: "终端", onSelect: () => (selected = "terminal") }]}
      />,
    );
    expect(menu).not.toBeNull();

    const item = menu!.querySelector<HTMLButtonElement>('[role="menuitem"]');
    act(() => item!.click());
    expect(selected).toBe("terminal");
    expect(document.querySelector(".row-menu-pop")).toBeNull();

    // 重新打开后点外部关闭
    act(() => container.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]')!.click());
    expect(document.querySelector(".row-menu-pop")).not.toBeNull();
    act(() => {
      document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });
    expect(document.querySelector(".row-menu-pop")).toBeNull();
  });

  it("触发器带 aria 状态", () => {
    const menu = openMenu(<RowMenu items={[{ label: "浏览器", onSelect: () => undefined }]} />);
    const trigger = container.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]')!;
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(menu!.id);
  });
});
