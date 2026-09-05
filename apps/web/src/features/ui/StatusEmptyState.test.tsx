import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";
import { EmptyState } from "./EmptyState";

describe("StatusBadge（展示契约）", () => {
  it("渲染 tone class 与文字，装饰点为 aria-hidden", () => {
    const html = renderToStaticMarkup(
      <StatusBadge label="正在回答" tone="active" pulse />,
    );
    expect(html).toContain("status-badge is-active is-pulsing");
    expect(html).toContain("正在回答");
    expect(html).toContain('aria-hidden="true"');
  });

  it("默认 neutral 且无 pulse", () => {
    const html = renderToStaticMarkup(<StatusBadge label="空闲" />);
    expect(html).toContain("status-badge is-neutral");
    expect(html).not.toContain("is-pulsing");
  });
});

describe("EmptyState（展示契约）", () => {
  it("标题/描述/动作与符号齐备（符号 aria-hidden）", () => {
    const html = renderToStaticMarkup(
      <EmptyState
        action={<button type="button">去上传</button>}
        desc="还没有内容"
        title="空空如也"
      />,
    );
    expect(html).toContain("<h3>空空如也</h3>");
    expect(html).toContain("还没有内容");
    expect(html).toContain("<button type=\"button\">去上传</button>");
    expect(html).toContain('aria-hidden="true"');
  });

  it("省略 desc/action 时不出多余节点", () => {
    const html = renderToStaticMarkup(<EmptyState title="无" />);
    expect(html).toContain("<h3>无</h3>");
    expect(html).not.toContain("<p>");
    expect(html).not.toContain("<button");
  });
});
