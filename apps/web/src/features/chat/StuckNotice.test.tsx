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

  // 批次 C0 复审 Important-1：这里**没有**"终态改新开一轮"的分支——卡住态只在在飞
  // run 上存在（后端 `_stuck_json` 在无在飞 run 时返回 None；前端终态事件即清空），
  // 所以两条出路照旧由 steer / 接管提供。会在终态存活的硬停横幅由
  // `RuntimeStatusStrip` 用真实在飞信号 `runningRunId` 处理。
  it("只传接管时只有「人工接管」，不会凭空多出一条「换一条路径」", () => {
    const html = renderToStaticMarkup(
      <StuckNotice onTakeOver={() => undefined} state={state} />,
    );
    expect(html).toContain("人工接管");
    expect(html).not.toContain("换一条路径");
    expect(html).toContain('aria-label="运行卡住"');
  });

  it("两条出路都不传时不渲染任何动作（读屏名照旧是「运行卡住」）", () => {
    const html = renderToStaticMarkup(<StuckNotice state={state} />);
    expect(html).toContain('aria-label="运行卡住"');
    expect(html).not.toContain("该运行已结束");
    expect(html).not.toContain("换一条路径");
    expect(html).not.toContain("人工接管");
  });
});
