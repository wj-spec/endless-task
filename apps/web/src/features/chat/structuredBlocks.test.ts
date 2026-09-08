import { describe, expect, it } from "vitest";
import {
  barRatios,
  formatMetricValue,
  isStructuredLanguage,
  isWideTable,
  parseChartSpec,
  parseMetricsSpec,
  parseStepsSpec,
} from "./structuredBlocks";

describe("structuredBlocks（A7 类型适配）", () => {
  it("识别支持的语言标记", () => {
    expect(isStructuredLanguage("chart")).toBe(true);
    expect(isStructuredLanguage("metrics")).toBe(true);
    expect(isStructuredLanguage("steps")).toBe(true);
    expect(isStructuredLanguage("python")).toBe(false);
  });

  it("解析图表规格并过滤坏点", () => {
    const spec = parseChartSpec(
      JSON.stringify({
        title: "指标对比",
        unit: "KB",
        series: [
          { label: "chat1.md", value: 78 },
          { label: "设计", value: 19 },
          { label: "坏点", value: "x" },
          { value: 5 },
        ],
      }),
    );
    expect(spec?.title).toBe("指标对比");
    expect(spec?.unit).toBe("KB");
    expect(spec?.series).toEqual([
      { label: "chat1.md", value: 78 },
      { label: "设计", value: 19 },
    ]);
  });

  it("无法解析或没有有效数据时返回 null（回退代码块）", () => {
    expect(parseChartSpec("not json")).toBeNull();
    expect(parseChartSpec(JSON.stringify({ series: [] }))).toBeNull();
    expect(parseChartSpec(JSON.stringify({ series: [{ label: "x" }] }))).toBeNull();
  });

  it("解析指标卡（数字值也接受）", () => {
    const spec = parseMetricsSpec(
      JSON.stringify({
        items: [
          { label: "总大小", value: "97KB" },
          { label: "文件数", value: 3, hint: "含附件" },
          { value: "缺 label" },
        ],
      }),
    );
    expect(spec?.items).toEqual([
      { label: "总大小", value: "97KB", hint: undefined },
      { label: "文件数", value: "3", hint: "含附件" },
    ]);
  });

  it("解析步骤清单", () => {
    const spec = parseStepsSpec(
      JSON.stringify({
        steps: [
          { title: "读取文件", done: true },
          { title: "汇总指标", detail: "按大小排序" },
          { detail: "缺标题" },
        ],
      }),
    );
    expect(spec?.steps).toEqual([
      { title: "读取文件", detail: undefined, done: true },
      { title: "汇总指标", detail: "按大小排序", done: false },
    ]);
  });

  it("条形比例按最大值归一", () => {
    expect(
      barRatios([
        { label: "a", value: 78 },
        { label: "b", value: 19 },
      ]),
    ).toEqual([1, 19 / 78]);
    expect(barRatios([{ label: "a", value: 0 }])).toEqual([0]);
  });

  it("数值格式化", () => {
    expect(formatMetricValue(3)).toBe("3");
    expect(formatMetricValue(1200)).toBe("1.2K");
    expect(formatMetricValue(2_500_000)).toBe("2.5M");
    expect(formatMetricValue(1.234)).toBe("1.23");
  });

  it("宽表判定", () => {
    expect(isWideTable(3, 1)).toBe(true);
    expect(isWideTable(2, 6)).toBe(true);
    expect(isWideTable(2, 2)).toBe(false);
  });
});
