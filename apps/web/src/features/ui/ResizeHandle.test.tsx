import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ResizeHandle } from "./ResizeHandle";

describe("ResizeHandle（分隔条）", () => {
  it("渲染可访问的 separator 并暴露当前值", () => {
    const html = renderToStaticMarkup(
      <ResizeHandle
        label="调整文件树宽度"
        max={360}
        min={180}
        onChange={() => undefined}
        value={240}
      />,
    );
    expect(html).toContain('role="separator"');
    expect(html).toContain('aria-orientation="vertical"');
    expect(html).toContain('aria-valuemin="180"');
    expect(html).toContain('aria-valuemax="360"');
    expect(html).toContain('aria-valuenow="240"');
    expect(html).toContain('tabindex="0"');
    expect(html).toContain('aria-label="调整文件树宽度"');
  });

  it("invert 模式只影响拖拽方向，不影响可访问属性", () => {
    const html = renderToStaticMarkup(
      <ResizeHandle
        invert
        label="调整侧边栏宽度"
        max={720}
        min={280}
        onChange={() => undefined}
        value={420}
      />,
    );
    expect(html).toContain('aria-valuenow="420"');
    expect(html).toContain("resize-handle");
  });
});
