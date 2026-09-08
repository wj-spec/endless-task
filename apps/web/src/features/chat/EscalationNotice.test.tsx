import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeV2Escalation } from "./apiTypes";
import { EscalationNotice } from "./EscalationNotice";

const state: RuntimeV2Escalation = {
  runId: "run_1",
  reason: "no_progress",
  summary: "连续 4 轮没有实质进展（工具调用 5 次、失败 3 次，新增文本 42 字）。",
  options: ["continue", "change_approach", "take_over"],
  progress: {
    modelTurns: 4,
    toolCalls: 5,
    toolFailures: 3,
    producedCharacters: 42,
    inputTokens: 900,
    outputTokens: 100,
  },
  budget: { usedTokens: 1000, limitTokens: 900, usedRatio: 1 },
  repeatedFailures: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      count: 3,
      safeMessage: "工具暂时不可用。",
    },
  ],
  failures: [],
  guidance: "失败记忆：read_file 已连续失败 3 次。请更换方法或参数，不要原样重复同一调用。",
  willStop: false,
};

describe("EscalationNotice（C4 终止与升级）", () => {
  it("展示原因、进展、卡点与失败记忆", () => {
    const html = renderToStaticMarkup(<EscalationNotice state={state} />);
    expect(html).toContain("需要你决定下一步");
    expect(html).toContain("连续无进展");
    expect(html).toContain("连续 4 轮没有实质进展");
    expect(html).toContain("已进行 4 轮、工具调用 5 次（失败 3 次）、新增文本 42 字");
    expect(html).toContain("read_file · temporary_unavailable · 连续 3 次");
    expect(html).toContain("不要原样重复");
  });

  it("已安全停止时标题说明状态", () => {
    const html = renderToStaticMarkup(
      <EscalationNotice state={{ ...state, willStop: true }} />,
    );
    expect(html).toContain("已安全停止");
  });

  it("三条出路只在有回调时渲染", () => {
    const bare = renderToStaticMarkup(<EscalationNotice state={state} />);
    expect(bare).not.toContain("换一条路径");
    expect(bare).not.toContain("人工接管");

    const html = renderToStaticMarkup(
      <EscalationNotice
        onChangeApproach={() => undefined}
        onContinue={() => undefined}
        onTakeOver={() => undefined}
        state={state}
      />,
    );
    expect(html).toContain("继续");
    expect(html).toContain("换一条路径");
    expect(html).toContain("人工接管");
  });

  it("options 缺失时回退到三条默认出路", () => {
    const html = renderToStaticMarkup(
      <EscalationNotice
        onChangeApproach={() => undefined}
        onContinue={() => undefined}
        onTakeOver={() => undefined}
        state={{ ...state, options: [] }}
      />,
    );
    expect(html).toContain("继续");
    expect(html).toContain("人工接管");
  });

  it("预算原因显示预算标签", () => {
    const html = renderToStaticMarkup(
      <EscalationNotice
        state={{ ...state, reason: "budget_exhausted" }}
      />,
    );
    expect(html).toContain("上下文预算将尽");
  });
});
