import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { WorkspaceFilesPane } from "./WorkspaceFilesPane";

describe("WorkspaceFilesPane（文件标签页）", () => {
  it("渲染工具栏与常驻的根目录标签", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFilesPane conversationId="conv_1" workspaceId="ws_1" workspaceName="myproj" />,
    );
    expect(html).toContain("显示降噪目录");
    expect(html).toContain("显示隐藏文件");
    expect(html).toContain("刷新");
    expect(html).toContain("终端");
    expect(html).toContain('role="tablist"');
    expect(html).toContain("myproj");
    // 根目录标签不显示关闭按钮
    expect(html).not.toContain("关闭 myproj");
  });

  it("不再使用树/双栏结构", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFilesPane conversationId="conv_1" workspaceId="ws_1" />,
    );
    expect(html).not.toContain('role="tree"');
    expect(html).not.toContain('role="separator"');
  });

  it("未绑定工作区时给出提示而不是请求", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFilesPane conversationId="conv_1" workspaceId="" />,
    );
    expect(html).toContain("该会话未绑定工作区");
  });
});
