import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { WorkspaceFilePreview } from "../chat/apiTypes";
import { FilePreviewBody } from "./FileViewer";

const preview = (over: Partial<WorkspaceFilePreview> = {}): WorkspaceFilePreview => ({
  path: "README.md",
  workspaceId: "w1",
  totalLines: 2,
  startLine: 1,
  endLine: 2,
  variant: "",
  lines: [
    { line: 1, text: "# 标题" },
    { line: 2, text: "正文" },
  ],
  ...over,
});

describe("FilePreviewBody（文件预览渲染）", () => {
  it("加载中与错误态", () => {
    const busy = renderToStaticMarkup(
      <FilePreviewBody busy error={null} path="README.md" preview={null} />,
    );
    expect(busy).toContain("正在读取…");

    const failed = renderToStaticMarkup(
      <FilePreviewBody
        busy={false}
        error="文件不存在。"
        onReveal={() => undefined}
        path="README.md"
        preview={null}
      />,
    );
    expect(failed).toContain('role="alert"');
    expect(failed).toContain("文件不存在。");
    expect(failed).toContain("用系统程序打开");
  });

  it("Markdown 走 MessageContent 渲染", () => {
    const html = renderToStaticMarkup(
      <FilePreviewBody busy={false} error={null} path="README.md" preview={preview()} />,
    );
    expect(html).toContain("<h1>标题</h1>");
    expect(html).toContain("message-copy");
  });

  it("代码文件走高亮代码块", () => {
    const html = renderToStaticMarkup(
      <FilePreviewBody
        busy={false}
        error={null}
        path="src/main.py"
        preview={preview({
          path: "src/main.py",
          lines: [
            { line: 1, text: "print('hi')" },
            { line: 2, text: "" },
          ],
        })}
      />,
    );
    expect(html).toContain("code-block");
    expect(html).toContain("python");
  });

  it("未知类型保留行号纯文本", () => {
    const html = renderToStaticMarkup(
      <FilePreviewBody
        busy={false}
        error={null}
        path="notes.txt"
        preview={preview({ path: "notes.txt" })}
      />,
    );
    expect(html).toContain("file-viewer-lineno");
    expect(html).toContain("正文");
  });

  it("超出预览窗口时提示截断", () => {
    const html = renderToStaticMarkup(
      <FilePreviewBody
        busy={false}
        error={null}
        path="notes.txt"
        preview={preview({ path: "notes.txt", totalLines: 5000, endLine: 2000 })}
      />,
    );
    expect(html).toContain("仅预览前 2000 行");
  });
});
