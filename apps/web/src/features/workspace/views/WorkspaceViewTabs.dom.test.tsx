// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { WorkspaceViewTabs } from "./WorkspaceViewTabs";
import { viewOf } from "./types";

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
  document.querySelectorAll(".row-menu-pop").forEach((node) => node.remove());
});

const render = (onCreate: (kind: string) => void = () => undefined) => {
  act(() =>
    root.render(
      <WorkspaceViewTabs
        activeKind="artifacts"
        onActivate={() => undefined}
        onClose={() => undefined}
        onCreate={onCreate}
        views={[viewOf("artifacts"), viewOf("files")]}
      />,
    ),
  );
};

describe("WorkspaceViewTabs「+」菜单（回归：被裁剪）", () => {
  it("菜单渲染到 body，不受标签条裁剪影响", () => {
    render();
    const add = container.querySelector<HTMLButtonElement>(
      'button[aria-label="新建视图"]',
    );
    expect(add).not.toBeNull();

    act(() => add!.click());

    const menu = document.querySelector<HTMLDivElement>(".row-menu-pop");
    expect(menu).not.toBeNull();
    // 关键断言：菜单不在标签条内部（否则会被 overflow 裁切）
    expect(container.contains(menu)).toBe(false);
    expect(menu!.parentElement).toBe(document.body);
    expect(menu!.style.position).not.toBe("absolute");
    expect(menu!.textContent).toContain("终端");
    expect(menu!.textContent).toContain("浏览器");
  });

  it("选择菜单项后回调并关闭", () => {
    const created: string[] = [];
    render((kind) => created.push(kind));
    act(() =>
      container
        .querySelector<HTMLButtonElement>('button[aria-label="新建视图"]')!
        .click(),
    );
    const items = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.row-menu-pop [role="menuitem"]'),
    );
    const terminalItem = items.find((item) => item.textContent === "终端")!;
    act(() => terminalItem.click());

    expect(created).toEqual(["terminals"]);
    expect(document.querySelector(".row-menu-pop")).toBeNull();
  });

  it("已打开的视图在菜单里标注", () => {
    render();
    act(() =>
      container
        .querySelector<HTMLButtonElement>('button[aria-label="新建视图"]')!
        .click(),
    );
    const labels = Array.from(
      document.querySelectorAll('.row-menu-pop [role="menuitem"]'),
    ).map((item) => item.textContent);
    expect(labels).toContain("产物（已打开）");
    expect(labels).toContain("文件（已打开）");
  });
});
