import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FormErrorSummary } from "./FormErrorSummary";

describe("FormErrorSummary（P1-4）", () => {
  it("无错误时不渲染", () => {
    const html = renderToStaticMarkup(<FormErrorSummary error={null} />);
    expect(html).toBe("");
  });

  it("渲染 role=alert、标题与错误文本，并带可聚焦 tabIndex", () => {
    const html = renderToStaticMarkup(
      <FormErrorSummary error="保存失败，请重试。" />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain('tabindex="-1"');
    expect(html).toContain("提交没有完成");
    expect(html).toContain("保存失败，请重试。");
  });

  it("数组错误逐条列出", () => {
    const html = renderToStaticMarkup(
      <FormErrorSummary error={["第一处问题", "第二处问题"]} heading="校验未通过" />,
    );
    expect(html).toContain("校验未通过");
    expect(html).toContain("<li>第一处问题</li>");
    expect(html).toContain("<li>第二处问题</li>");
  });
});
