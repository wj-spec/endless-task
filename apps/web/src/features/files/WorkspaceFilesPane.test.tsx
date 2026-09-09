import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { WorkspaceFilesPane } from "./WorkspaceFilesPane";

describe("WorkspaceFilesPane（文件面板工具栏）", () => {
  it("渲染隐藏文件开关、刷新与系统终端入口", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFilesPane conversationId="conv_1" workspaceId="ws_1" />,
    );
    expect(html).toContain("显示隐藏文件");
    expect(html).toContain("刷新");
    expect(html).toContain("终端");
  });

  it("未绑定工作区时给出提示而不是请求", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFilesPane conversationId="conv_1" workspaceId="" />,
    );
    expect(html).toContain("该会话未绑定工作区");
  });
});
