import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StructuredBlock } from "./StructuredBlock";

const fallback = <pre>原始内容</pre>;

describe("StructuredBlock（A7 类型适配渲染）", () => {
  it("chart 渲染为条形图并显示数值与单位", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock
        code={JSON.stringify({
          title: "指标对比",
          unit: "KB",
          series: [
            { label: "chat1.md", value: 78 },
            { label: "设计", value: 19 },
          ],
        })}
        fallback={fallback}
        language="chart"
      />,
    );
    expect(html).toContain("指标对比");
    expect(html).toContain("chat1.md");
    expect(html).toContain("78 KB");
    expect(html).toContain("structured-chart-bar");
    expect(html).not.toContain("原始内容");
  });

  it("metrics 渲染为指标卡", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock
        code={JSON.stringify({
          items: [
            { label: "总大小", value: "97KB" },
            { label: "文件数", value: 3, hint: "含附件" },
          ],
        })}
        fallback={fallback}
        language="metrics"
      />,
    );
    expect(html).toContain("总大小");
    expect(html).toContain("97KB");
    expect(html).toContain("含附件");
  });

  it("steps 渲染为步骤清单并标记完成", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock
        code={JSON.stringify({
          steps: [
            { title: "读取文件", done: true },
            { title: "汇总指标" },
          ],
        })}
        fallback={fallback}
        language="steps"
      />,
    );
    expect(html).toContain("读取文件");
    expect(html).toContain("✓");
    expect(html).toContain("汇总指标");
  });

  it("解析失败时回退原始代码块", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock code="not json" fallback={fallback} language="chart" />,
    );
    expect(html).toContain("原始内容");
    expect(html).not.toContain("structured-chart");
  });

  it("未知语言直接回退", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock code="print(1)" fallback={fallback} language="python" />,
    );
    expect(html).toContain("原始内容");
  });

  it("提供打开动作时渲染按钮", () => {
    const html = renderToStaticMarkup(
      <StructuredBlock
        code={JSON.stringify({ series: [{ label: "a", value: 1 }] })}
        fallback={fallback}
        language="chart"
        onOpenInPanel={() => undefined}
      />,
    );
    expect(html).toContain("在右侧打开");
  });
});
