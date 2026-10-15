/**
 * G-5（C1 余项 M6 残余）：用户可见面的"旧词回归"仍然**可以全绿**。
 *
 * 来源：`15-batch-c/c1r-review.md` §4 第 2 条 —— 复审实测把**用户可见**的
 * `title="分支对照"` 加进 `src/features/panel/AssistantPanel.tsx` 的根 `aside`
 * （该组件只在 `assistantOpen` 时挂载）⇒ 全量单测 **86/663 全绿**（M6）。同类未覆盖
 * 面还有 `SettingsOverlay`（`settingsOpen` 才挂）、`WorkspaceSettingsModal`、折叠态
 * `WorkspacePanel`——**逐组件 `innerHTML` 断言**这个形状天然盖不全（没被渲染到就断言不到）。
 *
 * 复审给的形状：把"旧词不出现在任何用户可见面"做成门禁可证的**字面量扫描**
 * （把 grep 变成测试）。这就是本文件。
 *
 * 规矩：
 * - 扫描面 = `apps/web/src/**\/*.{ts,tsx,css}` **排除** `*.test.ts` / `*.test.tsx`，
 *   外加 `apps/web/index.html`；
 * - **测试文件里允许**出现（它们是"断言旧词不存在"的**针**，见
 *   `ChatSurfaceHeader.test.tsx` / `mentionAppWiring.test.tsx`）；
 * - **生产源码里一律不允许** ⇒ 连**注释/docstring** 也顺带覆盖（C1 余项那 3 处
 *   就在生产源码里）；
 * - **不**扫 `dist/` 产物（要先 `build`、且脆弱）；
 * - 扫描面本身有**下界断言**：把文件集删空 / 过滤过头时立刻红，防"空扫即绿"；
 * - **命中判据本身也有用例内正向对照**（G-5 复审 M-5）：扫不到词有两种原因——
 *   "真的没有了"与"扫描器被改坏了"，旧写法对后者**永远绿**。所以判据
 *   （`containsOldWord`）与扫描管线（`oldWordHits`）都抽成具名函数，并**在用例里**
 *   拿一份合成的含旧词文本 + 一个已知命中文件喂它们 ⇒ 改坏判据时用例自己红。
 *
 * ⚠️ 这是"源码字面量"级守卫：它证伪的是"词还在生产源码里"，**不是**"词一定不会
 * 渲染到屏幕上"（例如从后端数据里拼出来的文案它管不着）。它覆盖的正是逐组件
 * `innerHTML` 断言盖不到的那部分（未挂载的组件、注释、CSS 内容）。
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** 本文件在 `apps/web/src/` 下。 */
const SRC_ROOT = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = resolve(SRC_ROOT, "..");

/** C1/P0-B 收口时被统一掉的旧词：任何用户可见/可听面都不许再出现。 */
const OLD_WORD = "分支对照";

const SCANNED_EXTENSIONS = [".ts", ".tsx", ".css"] as const;

const isTestFile = (path: string) => /\.test\.tsx?$/.test(path);

/**
 * 命中判据：一份**文本**里是否出现旧词。
 *
 * 真实扫描（`oldWordHits`）与**用例内**的正向对照共用这**同一条**（G-5 复审 M-5）。
 * 旧版把 `.includes(OLD_WORD)` 直接写在 `filter` 里：把它改成 `() => false` 之后
 * 扫描恒定"零命中"，而用例**照样全绿**——"扫描器被改坏"与"旧词真的没了"不可分。
 */
const containsOldWord = (text: string) => text.includes(OLD_WORD);

/**
 * 扫描管线的一步：**文件集合 → 命中文件列表**（相对 `apps/web`）。
 *
 * 真实扫描与用例内的正向对照都走这**一个**函数 ⇒ "判据/过滤器被改成永不命中"时，
 * 用例内的对照会**先红**，不会再出现"扫描器坏了但门禁全绿"。
 */
const oldWordHits = (paths: string[]) =>
  paths
    .filter((path) => containsOldWord(readFileSync(path, "utf8")))
    .map((path) => relative(WEB_ROOT, path));

const walk = (dir: string, acc: string[] = []): string[] => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
};

/** 非测试源码 + `index.html`（顺序稳定，断言输出可读）。 */
const scannedFiles = (): string[] =>
  [
    ...walk(join(WEB_ROOT, "src")).filter(
      (path) =>
        SCANNED_EXTENSIONS.some((extension) => path.endsWith(extension)) &&
        !isTestFile(path),
    ),
    join(WEB_ROOT, "index.html"),
  ].sort();

describe("G-5：旧词「分支对照」不出现在 apps/web 的非测试源码里（源码字面量扫描）", () => {
  it("非测试源码（src/**/*.{ts,tsx,css} 排除 *.test.*，外加 index.html）里「分支对照」出现 0 次", () => {
    const files = scannedFiles();

    // 下界断言（防"把扫描面删空也绿"）：当前 152 个源码文件 + index.html = 153。
    // 门槛留一点余量，但远高于"只剩两三个文件"。
    expect(files.length).toBeGreaterThan(140);
    // 扫描面必须真的包含生产源码与 `index.html`（而不是被过滤成只剩零碎文件）。
    expect(files.some((path) => path.endsWith("App.tsx"))).toBe(true);
    expect(files.some((path) => path.endsWith("index.html"))).toBe(true);
    expect(files.some((path) => path.endsWith(".css"))).toBe(true);
    // 排除逻辑本身也要被钉住：一条测试文件都不能混进扫描面。
    expect(files.filter((path) => isTestFile(path))).toEqual([]);

    // 【用例内正向对照】（G-5 复审 M-5）扫描器必须**自证有牙**——否则"扫到 0 个"
    // 既可能是"旧词真的没了"，也可能是"判据/过滤器被改坏了"，两者不可分。
    // ① 合成的含旧词文本 ⇒ 判据必须命中；不含旧词的文本 ⇒ 必须不命中（防"永远命中"）。
    expect(containsOldWord(`<aside title="${OLD_WORD}" />`)).toBe(true);
    expect(containsOldWord("当前主线")).toBe(false);
    // ② 已知命中文件 ⇒ **同一条扫描管线**必须把它报出来。本测试文件自己必然含
    //    `OLD_WORD` 的字面量（常量赋值就在上面），所以它是个稳定的"已知命中文件"。
    const SELF_PATH = fileURLToPath(import.meta.url);
    expect(oldWordHits([SELF_PATH])).toEqual([relative(WEB_ROOT, SELF_PATH)]);

    const hits = oldWordHits(files);

    // 命中即列出**具体文件**（这条断言的失败信息本身就是定位工具）。
    expect(hits).toEqual([]);
  });
});
