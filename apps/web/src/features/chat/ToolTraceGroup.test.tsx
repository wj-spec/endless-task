import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeToolItem } from "./runtimeTrace";
import { ToolTraceGroup } from "./ToolTraceGroup";

const tool = (
  id: string,
  over: Partial<RuntimeToolItem> = {},
): RuntimeToolItem => ({
  key: id,
  toolName: "read_text_file",
  phase: "completed",
  isError: false,
  hasArgs: false,
  hasResult: true,
  ...over,
});

describe("ToolTraceGroup（状态展示默认折叠）", () => {
  it("默认折叠：只显示摘要行，不渲染工具卡", () => {
    const html = renderToStaticMarkup(
      <ToolTraceGroup tools={[tool("e1"), tool("e2")]} />,
    );
    expect(html).toContain("工具执行");
    expect(html).toContain("2 步");
    expect(html).toContain("全部完成");
    expect(html).toContain('aria-expanded="false"');
    // 工具卡正文不在折叠态里。
    expect(html).not.toContain("runtime-tool-card");
    expect(html).not.toContain("tool-trace-body");
  });

  it("失败数在摘要里可见并用 danger 语气", () => {
    const html = renderToStaticMarkup(
      <ToolTraceGroup
        tools={[tool("e1"), tool("e2", { isError: true, phase: "failed" })]}
      />,
    );
    expect(html).toContain("1 个失败");
    expect(html).toContain("is-danger");
  });

  it("运行中显示执行中", () => {
    const html = renderToStaticMarkup(
      <ToolTraceGroup active tools={[tool("e1", { phase: "running" })]} />,
    );
    expect(html).toContain("执行中");
    expect(html).toContain("is-warn");
  });

  it("空工具列表不渲染", () => {
    expect(renderToStaticMarkup(<ToolTraceGroup tools={[]} />)).toBe("");
  });
});
