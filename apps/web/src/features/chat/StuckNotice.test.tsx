import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeV2StuckState } from "./apiTypes";
import { StuckNotice } from "./StuckNotice";

const state: RuntimeV2StuckState = {
  runId: "run_1",
  level: "remind",
  detector: "repeated_failure",
  reasons: ["`read_file` 已连续失败 2 次（temporary_unavailable）"],
  repeatedFailures: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      count: 2,
      safeMessage: "工具暂时不可用。",
    },
  ],
  attempts: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      attempt: 2,
      toolExecutionId: "exec_2",
    },
  ],
  guidance: "失败记忆：`read_file` 已连续失败 2 次。请更换方法或参数，不要原样重复同一调用。",
};

describe("StuckNotice（C2 失败记忆）", () => {
  it("展示卡住标题、失败原因与失败记忆", () => {
    const html = renderToStaticMarkup(<StuckNotice state={state} />);
    expect(html).toContain("可能卡住了");
    expect(html).toContain("read_file");
    expect(html).toContain("temporary_unavailable");
    expect(html).toContain("已记录 1 次失败尝试");
    expect(html).toContain("不要原样重复");
  });

  it("没有 reasons 时回退到结构化失败摘要", () => {
    const html = renderToStaticMarkup(
      <StuckNotice state={{ ...state, reasons: [], guidance: "" }} />,
    );
    expect(html).toContain("read_file · temporary_unavailable · 连续 2 次");
  });

  it("有回调时才渲染操作按钮", () => {
    const bare = renderToStaticMarkup(<StuckNotice state={state} />);
    expect(bare).not.toContain("换一条路径");
    expect(bare).not.toContain("人工接管");

    const withActions = renderToStaticMarkup(
      <StuckNotice
        onSteer={() => undefined}
        onTakeOver={() => undefined}
        state={state}
      />,
    );
    expect(withActions).toContain("换一条路径");
    expect(withActions).toContain("人工接管");
  });

  it("按级别切换样式类与标题", () => {
    const html = renderToStaticMarkup(
      <StuckNotice state={{ ...state, level: "restrict" }} />,
    );
    expect(html).toContain("stuck-notice-restrict");
    expect(html).toContain("连续失败，建议换一条路径");
  });
});
