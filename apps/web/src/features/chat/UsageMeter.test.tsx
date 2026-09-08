import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeV2UsageSummary } from "./apiTypes";
import { UsageMeter } from "./UsageMeter";

const usage: RuntimeV2UsageSummary = {
  runId: "run_1",
  turns: 1,
  inputTokens: 1200,
  outputTokens: 480,
  totalTokens: 1680,
  costUsd: 0.004,
  costPriced: true,
  unpricedTurns: 0,
  priceRevision: "test-1",
  models: ["scripted/scripted-model"],
  durationMs: 8300,
  firstTokenLatencyMs: null,
};

describe("UsageMeter（C5 成本/延迟可见）", () => {
  it("展示 token/成本/耗时", () => {
    const html = renderToStaticMarkup(<UsageMeter usage={usage} />);
    expect(html).toContain("本轮");
    expect(html).toContain("1.2K in / 480 out");
    expect(html).toContain("~$0.0040");
    expect(html).toContain("8.3s");
  });

  it("未定价时标注未定价且加弱化类", () => {
    const html = renderToStaticMarkup(
      <UsageMeter usage={{ ...usage, costUsd: null, costPriced: false }} />,
    );
    expect(html).toContain("未定价");
    expect(html).toContain("usage-meter-unpriced");
    expect(html).not.toContain("$");
  });

  it("部分轮次未定价时给出提示", () => {
    const html = renderToStaticMarkup(
      <UsageMeter usage={{ ...usage, unpricedTurns: 2 }} />,
    );
    expect(html).toContain("2 轮未定价");
  });

  it("title 说明定价口径", () => {
    const html = renderToStaticMarkup(<UsageMeter usage={usage} />);
    expect(html).toContain("成本为估算");
    expect(html).toContain("定价表 test-1");
  });
});
