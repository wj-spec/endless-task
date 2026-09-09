import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import { FileTree } from "./FileTree";

const entry = (over: Partial<WorkspaceTreeEntry>): WorkspaceTreeEntry => ({
  name: "file",
  relativePath: "file",
  kind: "file",
  size: 0,
  isHidden: false,
  ...over,
});

const baseProps = {
  expanded: [] as string[],
  loading: [] as string[],
  errors: {} as Record<string, string>,
  truncated: {} as Record<string, boolean>,
  onToggle: () => undefined,
  onOpen: () => undefined,
};

const rootEntries: WorkspaceTreeEntry[] = [
  entry({ name: "src", relativePath: "src", kind: "directory" }),
  entry({ name: "README.md", relativePath: "README.md", size: 2048 }),
];

describe("FileTree（工作区文件树）", () => {
  it("hideNoisy 过滤 node_modules 等降噪目录", () => {
    const entries = [
      entry({ name: "src", relativePath: "src", kind: "directory" }),
      entry({ name: "node_modules", relativePath: "node_modules", kind: "directory" }),
    ];
    const html = renderToStaticMarkup(
      <FileTree {...baseProps} children={{ "": entries }} hideNoisy />,
    );
    expect(html).toContain("src");
    expect(html).not.toContain("node_modules");
  });

  it("选中项带 is-selected，文件显示扩展名标记", () => {
    const html = renderToStaticMarkup(
      <FileTree
        {...baseProps}
        children={{ "": rootEntries }}
        selectedPath="README.md"
      />,
    );
    expect(html).toContain("is-selected");
    expect(html).toContain("MD");
  });


  it("渲染根层条目：目录在前、文件带大小", () => {
    const html = renderToStaticMarkup(
      <FileTree {...baseProps} children={{ "": rootEntries }} />,
    );
    expect(html).toContain('role="tree"');
    expect(html).toContain('role="treeitem"');
    expect(html).toContain("src");
    expect(html).toContain("README.md");
    expect(html).toContain("2.0 KB");
    // 折叠态目录带 aria-expanded=false 且不渲染子层。
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('aria-expanded="true"');
  });

  it("展开的目录渲染子层并标注层级", () => {
    const html = renderToStaticMarkup(
      <FileTree
        {...baseProps}
        children={{
          "": rootEntries,
          src: [entry({ name: "main.py", relativePath: "src/main.py" })],
        }}
        expanded={["src"]}
      />,
    );
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain("main.py");
    expect(html).toContain('aria-level="2"');
  });

  it("降噪目录弱化显示", () => {
    const html = renderToStaticMarkup(
      <FileTree
        {...baseProps}
        children={{
          "": [entry({ name: "node_modules", relativePath: "node_modules", kind: "directory" })],
        }}
      />,
    );
    expect(html).toContain("is-noisy");
  });

  it("空目录、加载中与截断提示", () => {
    const empty = renderToStaticMarkup(<FileTree {...baseProps} children={{ "": [] }} />);
    expect(empty).toContain("空目录");

    const busy = renderToStaticMarkup(
      <FileTree {...baseProps} children={{}} loading={[""]} />,
    );
    expect(busy).toContain("正在读取…");

    const truncated = renderToStaticMarkup(
      <FileTree
        {...baseProps}
        children={{ "": rootEntries }}
        truncated={{ "": true }}
      />,
    );
    expect(truncated).toContain("仅显示前 200 项");
  });

  it("目录加载失败显示错误行", () => {
    const html = renderToStaticMarkup(
      <FileTree
        {...baseProps}
        children={{ "": rootEntries }}
        errors={{ "": "目录加载失败。" }}
      />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("目录加载失败。");
  });
});
