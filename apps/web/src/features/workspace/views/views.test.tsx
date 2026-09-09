import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BrowserPlaceholder } from "./BrowserPlaceholder";
import { WorkspaceViewTabs } from "./WorkspaceViewTabs";
import { VIEW_LABELS, VIEW_MENU, viewOf } from "./types";
import { parsePersisted } from "./useWorkspaceViews";

describe("视图注册表", () => {
  it("菜单顺序与标签固定", () => {
    expect(VIEW_MENU).toEqual(["artifacts", "files", "terminals", "browser"]);
    expect(VIEW_LABELS.artifacts).toBe("产物");
    expect(viewOf("terminals").subtitle).toContain("终端");
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
