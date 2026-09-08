import { describe, expect, it } from "vitest";
import type { RuntimeV2UsageSummary } from "./apiTypes";
import {
  formatCost,
  formatDuration,
  formatTokens,
  usageLine,
  usageTooltip,
} from "./usageCost";

const usage: RuntimeV2UsageSummary = {
  runId: "run_1",
  turns: 2,
  inputTokens: 1200,
  outputTokens: 480,
  totalTokens: 1680,
  costUsd: 0.004,
  costPriced: true,
  unpricedTurns: 0,
  priceRevision: "test-1",
  models: ["scripted/scripted-model"],
  durationMs: 8300,
  firstTokenLatencyMs: 700,
};

describe("usageCost 格式化（C5）", () => {
  it("token 用 K/M 缩写", () => {
    expect(formatTokens(480)).toBe("480");
    expect(formatTokens(1200)).toBe("1.2K");
    expect(formatTokens(12_000)).toBe("12K");
    expect(formatTokens(1_200_000)).toBe("1.2M");
    expect(formatTokens(0)).toBe("0");
  });

  it("成本小额保留 4 位，未定价明确标注", () => {
    expect(formatCost(0.004, true)).toBe("~$0.0040");
    expect(formatCost(1.2345, true)).toBe("~$1.23");
    expect(formatCost(null, false)).toBe("未定价");
    expect(formatCost(0.5, false)).toBe("未定价");
  });

  it("耗时按毫秒/秒/分格式化", () => {
    expect(formatDuration(420)).toBe("420ms");
    expect(formatDuration(8300)).toBe("8.3s");
    expect(formatDuration(125_000)).toBe("2m5s");
    expect(formatDuration(null)).toBe("—");
  });

  it("一行摘要包含 in/out、成本与耗时", () => {
    expect(usageLine(usage)).toBe(
      "1.2K in / 480 out · ~$0.0040 · 8.3s · 首字 700ms",
    );
  });

  it("未定价时摘要显示未定价而非假数字", () => {
    const line = usageLine({ ...usage, costUsd: null, costPriced: false });
    expect(line).toContain("未定价");
    expect(line).not.toContain("$");
  });

  it("定价口径说明区分已定价/未定价/部分未定价", () => {
    expect(usageTooltip(usage)).toContain("定价表 test-1");
    expect(usageTooltip({ ...usage, costPriced: false })).toContain("未在定价表");
    expect(usageTooltip({ ...usage, unpricedTurns: 1 })).toContain(
      "1 轮未定价",
    );
  });
});
