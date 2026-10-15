import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BrowserPlaceholder } from "./BrowserPlaceholder";
import { WorkspaceViewTabs } from "./WorkspaceViewTabs";
import { VIEW_LABELS, VIEW_MENU, VIEW_HINTS, VIEW_SUBTITLES, isWorkspaceViewKind, viewOf } from "./types";
import { parsePersisted } from "./useWorkspaceViews";

describe("视图注册表", () => {
  it("菜单顺序与标签固定", () => {
    expect(VIEW_MENU).toEqual([
      "artifacts",
      "conversations",
      "files",
      "changes",
      "terminals",
      "browser",
    ]);
    expect(VIEW_LABELS.artifacts).toBe("产物");
    expect(VIEW_LABELS.changes).toBe("变更");
    expect(viewOf("terminals").subtitle).toContain("终端");
  });

  // C4：新 kind 必须在**四个**地方同时就位（类型/标签/菜单/守卫）。少一处就会
  // 出现"页签在但记忆不回来"或"「+」菜单里没有它"的半接线。
  it("C4：会话视图在注册表里（标签 / 菜单位置 / 副标题 / 提示）", () => {
    expect(VIEW_LABELS.conversations).toBe("会话");
    expect(viewOf("conversations").title).toBe("会话");
    expect(VIEW_SUBTITLES.conversations).toContain("临时对话");
    expect(VIEW_HINTS.conversations).toContain("分支树");
    expect(VIEW_MENU).toContain("conversations");
  });

  it("C4：isWorkspaceViewKind 认「会话」且仍然拒绝非 kind", () => {
    expect(isWorkspaceViewKind("conversations")).toBe(true);
    expect(isWorkspaceViewKind("会话")).toBe(false);
  });

  it("C4：会话视图能被持久化记忆（守卫漏了就会被静默丢掉）", () => {
    expect(
      parsePersisted(JSON.stringify({ open: ["conversations"], active: "conversations" })),
    ).toEqual({ open: ["conversations"], active: "conversations" });
  });
});

describe("parsePersisted（视图记忆）", () => {
  it("空值回落到产物视图", () => {
    expect(parsePersisted(null)).toEqual({ open: ["artifacts"], active: "artifacts" });
  });

  it("过滤非法类型并回落到首个已打开视图", () => {
    expect(
      parsePersisted(JSON.stringify({ open: ["files", "nope"], active: "browser" })),
    ).toEqual({ open: ["files"], active: "files" });
  });

  it("非法 JSON 不抛错", () => {
    expect(parsePersisted("{not json")).toEqual({
      open: ["artifacts"],
      active: "artifacts",
    });
  });
});

describe("WorkspaceViewTabs（标签条）", () => {
  const views = [viewOf("artifacts"), viewOf("files")];

  it("渲染 tablist / tab 与关闭按钮", () => {
    const html = renderToStaticMarkup(
      <WorkspaceViewTabs
        activeKind="files"
        onActivate={() => undefined}
        onClose={() => undefined}
        onCreate={() => undefined}
        views={views}
      />,
    );
    expect(html).toContain('role="tablist"');
    expect(html).toContain('role="tab"');
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain("关闭产物");
    expect(html).toContain("新建视图");
  });

  it("只有一个视图时不显示关闭按钮", () => {
    const html = renderToStaticMarkup(
      <WorkspaceViewTabs
        activeKind="artifacts"
        onActivate={() => undefined}
        onClose={() => undefined}
        onCreate={() => undefined}
        views={[viewOf("artifacts")]}
      />,
    );
    expect(html).not.toContain("关闭产物");
  });
});

describe("BrowserPlaceholder（浏览器占位）", () => {
  it("说明规划中并给出降级入口", () => {
    const html = renderToStaticMarkup(
      <BrowserPlaceholder onOpenExternal={() => undefined} />,
    );
    expect(html).toContain("浏览器视图规划中");
    expect(html).toContain("用系统浏览器打开");
  });
});
