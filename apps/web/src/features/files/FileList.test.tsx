import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import { FileList } from "./FileList";

const entry = (over: Partial<WorkspaceTreeEntry>): WorkspaceTreeEntry => ({
  name: "file",
  relativePath: "file",
  kind: "file",
  size: 0,
  isHidden: false,
  ...over,
});

const base = {
  busy: false,
  error: null as string | null,
  truncated: false,
  hideNoisy: false,
  onOpenDirectory: () => undefined,
  onOpenFile: () => undefined,
};

const entries = [
  entry({ name: "src", relativePath: "src", kind: "directory" }),
  entry({ name: "README.md", relativePath: "README.md", size: 2048 }),
  entry({ name: "main.ts", relativePath: "main.ts", size: 512 }),
  entry({ name: "data.json", relativePath: "data.json", size: 128 }),
  entry({ name: "logo.png", relativePath: "logo.png", size: 4096 }),
  entry({ name: "node_modules", relativePath: "node_modules", kind: "directory" }),
];

describe("FileList（同一级平铺）", () => {
  it("渲染直接子项：目录标注类型、文件带大小与分类图标", () => {
    const html = renderToStaticMarkup(<FileList {...base} entries={entries} />);
    expect(html).toContain('aria-label="目录内容"');
    expect(html).toContain("README.md");
    expect(html).toContain("2.0 KB");
    expect(html).toContain("目录");
    // 分类图标类名
    expect(html).toContain("is-markdown");
    expect(html).toContain("is-code");
    expect(html).toContain("is-data");
    expect(html).toContain("is-image");
    expect(html).toContain("is-folder");
    // 平铺：没有嵌套的 group/treeitem 结构
    expect(html).not.toContain('role="treeitem"');
    expect(html).not.toContain('role="group"');
  });

  it("隐藏降噪目录", () => {
    const html = renderToStaticMarkup(
      <FileList {...base} entries={entries} hideNoisy />,
    );
    expect(html).not.toContain("node_modules");
    expect(html).toContain("src");
  });

  it("空目录 / 读取中 / 错误态", () => {
    expect(renderToStaticMarkup(<FileList {...base} entries={[]} />)).toContain(
      "空目录",
    );
    expect(
      renderToStaticMarkup(<FileList {...base} busy entries={[]} />),
    ).toContain("正在读取");
    expect(
      renderToStaticMarkup(<FileList {...base} entries={[]} error="目录加载失败。" />),
    ).toContain('role="alert"');
  });
});
