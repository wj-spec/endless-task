import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TerminalSnapshot } from "../chat/apiTypes";
import { TerminalWorkspace } from "./TerminalPane";

const session = (over: Partial<TerminalSnapshot> = {}): TerminalSnapshot => ({
  sessionId: "term_abcdef123456",
  workspaceId: "ws_1",
  pid: 4242,
  cwd: "/tmp/ws",
  name: "",
  createdAt: 0,
  status: { kind: "running", exitCode: null, signal: null },
  rows: 40,
  cols: 160,
  ...over,
});

const base = {
  workspaceId: "ws_1",
  busy: false,
  error: null as string | null,
  onCreate: () => undefined,
  onSelect: () => undefined,
  onCloseSession: () => undefined,
};

describe("TerminalWorkspace（终端页签）", () => {
  it("没有会话时给出新建入口与空态说明", () => {
    const html = renderToStaticMarkup(
      <TerminalWorkspace {...base} activeId={null} sessions={[]} />,
    );
    expect(html).toContain("新建终端");
    expect(html).toContain("还没有终端会话");
  });

  it("会话列表展示名称与退出状态", () => {
    const html = renderToStaticMarkup(
      <TerminalWorkspace
        {...base}
        activeId="term_abcdef123456"
        sessions={[
          session({ name: "main" }),
          session({
            sessionId: "term_zzz999999",
            status: { kind: "exited", exitCode: 0, signal: null },
          }),
        ]}
      />,
    );
    expect(html).toContain('role="tablist"');
    expect(html).toContain("main");
    expect(html).toContain("已退出");
    expect(html).toContain("关闭当前");
  });

  it("错误态可见", () => {
    const html = renderToStaticMarkup(
      <TerminalWorkspace
        {...base}
        activeId={null}
        error="创建终端失败。"
        sessions={[]}
      />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("创建终端失败。");
  });
});
