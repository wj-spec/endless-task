// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";
import { Terminal } from "@xterm/xterm";
import { buildTerminalTheme } from "./TerminalPane";

// vitest 默认不处理 CSS 导入，直接按 cwd（apps/web）从磁盘读源码/样式做断言。
const readSource = (relative: string) =>
  readFileSync(resolve(process.cwd(), relative), "utf8");
const xtermBaseCss = readSource("node_modules/@xterm/xterm/css/xterm.css");
const panelCss = readSource("src/styles/panel.css");
const paneSource = readSource("src/features/terminal/TerminalPane.tsx");

/**
 * 回归：终端里用鼠标选中文字时，选择色会把文字完全盖住。
 *
 * 根因：xterm 的选择浮层 `.xterm-selection` 是 `position:absolute; z-index:1`，
 * 盖在文本行（静态定位）之上；被选中的文本 span 只有带上基础样式里的
 * `.xterm-decoration-top { z-index:2; position:relative }` 才会压回浮层之上。
 * 也就是说 **必须加载 `@xterm/xterm/css/xterm.css`**，否则选中即遮挡。
 */
const stubMatchMedia = () => {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
};

let host: HTMLDivElement;
let style: HTMLStyleElement;

beforeEach(() => {
  stubMatchMedia();
  host = document.createElement("div");
  document.body.appendChild(host);
  style = document.createElement("style");
  style.textContent = xtermBaseCss;
  document.head.appendChild(style);
});

afterEach(() => {
  host.remove();
  style.remove();
});

const mountAndSelect = async () => {
  const terminal = new Terminal({ theme: buildTerminalTheme() });
  terminal.open(host);
  terminal.write("hello world\r\nsecond line\r\n");
  await new Promise((resolve) => setTimeout(resolve, 60));
  terminal.selectAll();
  await new Promise((resolve) => setTimeout(resolve, 60));
  return terminal;
};

describe("终端选中文字（S15 回归）", () => {
  it("TerminalPane 必须加载 xterm 基础样式（否则选中即遮挡）", () => {
    expect(paneSource).toContain("@xterm/xterm/css/xterm.css");
  });

  it("基础样式把视口背景写死为黑，产品样式必须兜底覆盖", () => {
    expect(xtermBaseCss).toMatch(/\.xterm-viewport\s*\{[^}]*background-color:\s*#000/);
    expect(panelCss).toMatch(
      /\.terminal-pane-host \.xterm \.xterm-viewport\s*\{[^}]*background-color/,
    );
  });

  it("基础样式里存在让文字压住选择浮层的规则", () => {
    expect(xtermBaseCss).toContain(".xterm-decoration-top");
    expect(xtermBaseCss).toMatch(/\.xterm-decoration-top\s*\{[^}]*z-index:\s*2/);
    expect(xtermBaseCss).toMatch(/\.xterm-decoration-top\s*\{[^}]*position:\s*relative/);
  });

  it("被选中的文本 span 带 xterm-decoration-top，且层级高于选择浮层", async () => {
    await mountAndSelect();

    const selectedSpan = host.querySelector(".xterm-rows .xterm-decoration-top");
    expect(selectedSpan).not.toBeNull();

    const overlay = host.querySelector(".xterm-selection") as HTMLElement;
    expect(overlay).not.toBeNull();
    expect(getComputedStyle(overlay).zIndex).toBe("1");

    // 关键：选中文本的 z-index 必须大于浮层，否则被选择色完全遮挡
    expect(getComputedStyle(selectedSpan as HTMLElement).zIndex).toBe("2");
    expect(getComputedStyle(selectedSpan as HTMLElement).position).toBe("relative");
  });

  it("选择色本身是实色（因此必须依赖上面的层级，不能靠透明度）", async () => {
    await mountAndSelect();
    const overlayDiv = host.querySelector(".xterm-selection div") as HTMLElement;
    expect(overlayDiv).not.toBeNull();
    const rules = Array.from(document.querySelectorAll("style"))
      .map((node) => node.textContent ?? "")
      .join("\n");
    const match = rules.match(/\.xterm-selection div \{[^}]*background-color:\s*([^;]+);/);
    expect(match).not.toBeNull();
    // xterm 会把选择色与终端背景混合成实色（rgba 形式也应为 alpha=1）
    expect(match![1].trim()).toMatch(/^#|rgb\(/);
  });
});
