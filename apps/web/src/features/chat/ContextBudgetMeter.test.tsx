import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeV2Snapshot } from "./apiTypes";
import { ContextBudgetMeter } from "./ContextBudgetMeter";

const budget = (
  overrides: Partial<NonNullable<RuntimeV2Snapshot["contextBudget"]>> = {},
) => ({
  limitTokens: 131_072,
  usedTokens: 12_000,
  usedRatio: 12_000 / 131_072,
  remainingTokens: 119_072,
  cumulativeTokens: 45_000,
  ...overrides,
});

describe("ContextBudgetMeter（A2 环形指示器）", () => {
  it("渲染为环形，并给出百分比与语义色类", () => {
    const html = renderToStaticMarkup(
      <ContextBudgetMeter budget={budget()} />,
    );
    expect(html).toContain("context-ring");
    expect(html).toContain("context-ring-track");
    expect(html).toContain("context-ring-fill");
    expect(html).toContain(">9<"); // 9%
    expect(html).toContain("is-ok");
    expect(html).not.toContain("context-ring-hint");
  });

  it("接近上限时切换为 warn，并显示当前/上限/剩余", () => {
    const html = renderToStaticMarkup(
      <ContextBudgetMeter
        budget={budget({
          usedTokens: 110_000,
          usedRatio: 110_000 / 131_072,
          remainingTokens: 21_072,
          cumulativeTokens: 180_000,
        })}
      />,
    );
    expect(html).toContain("is-warn");
    expect(html).toContain("上下文 84%");
    expect(html).toContain("当前 110.0K / 131.1K");
    expect(html).toContain("剩余 21.1K");
    expect(html).toContain("本轮累计 180.0K");
  });

  it("超过 95% 时为 high 并提示将满", () => {
    const html = renderToStaticMarkup(
      <ContextBudgetMeter
        budget={budget({
          usedTokens: 130_000,
          usedRatio: 0.99,
          remainingTokens: 1_072,
        })}
      />,
    );
    expect(html).toContain("is-high");
    expect(html).toContain("将满");
    expect(html).toContain(">99<");
  });

  it("比例越界时被夹紧到 0–100%", () => {
    const over = renderToStaticMarkup(
      <ContextBudgetMeter budget={budget({ usedRatio: 3 })} />,
    );
    expect(over).toContain(">100<");
    const under = renderToStaticMarkup(
      <ContextBudgetMeter budget={budget({ usedRatio: -1 })} />,
    );
    expect(under).toContain(">0<");
  });

  it("没有累计值时省略累计提示", () => {
    const html = renderToStaticMarkup(
      <ContextBudgetMeter
        budget={budget({ cumulativeTokens: undefined })}
      />,
    );
    expect(html).not.toContain("本轮累计");
  });

  it("没有预算数据时不渲染", () => {
    expect(renderToStaticMarkup(<ContextBudgetMeter budget={undefined} />)).toBe(
      "",
    );
  });
});
