import { describe, expect, it } from "vitest";
import type { RuntimeV2ProductEvent } from "./apiTypes";
import { runStageLabel } from "./runtimeStage";

const event = (over: Partial<RuntimeV2ProductEvent>): RuntimeV2ProductEvent => ({
  eventId: "e",
  eventSeq: 1,
  type: "message.updated",
  conversationId: "c",
  laneId: "lane",
  runId: "run-1",
  createdAt: "2026-09-06T00:00:00Z",
  data: {},
  ...over,
});

const plan = {
  title: "调研计划",
  steps: [
    { title: "步骤一", status: "completed" },
    { title: "步骤二：起草", status: "in_progress" },
  ],
  currentStepIndex: 1,
};

describe("runStageLabel（S-A1 阶段活动行）", () => {
  it("非运行或缺失 runId 不显示", () => {
    expect(runStageLabel({ runId: null, running: true })).toBeNull();
    expect(runStageLabel({ runId: "run-1", running: false, plan })).toBeNull();
  });

  it("纯问答（无工具/整理/计划）→ null，不打扰", () => {
    expect(
      runStageLabel({
        runId: "run-1",
        running: true,
        events: [
          event({ type: "message.updated", data: { delta: "你好" } }),
        ],
      }),
    ).toBeNull();
  });

  it("运行中工具最优先", () => {
    const label = runStageLabel({
      runId: "run-1",
      running: true,
      events: [
        event({
          type: "tool_execution.started",
          data: { toolExecutionId: "exec", status: "started", toolName: "read_text_file" },
        }),
      ],
      plan,
    });
    expect(label).toBe("正在执行「read_text_file」…");
  });

  it("整理上下文优先于计划步", () => {
    const label = runStageLabel({
      runId: "run-1",
      running: true,
      events: [
        event({ type: "context.compaction_started", data: {} }),
      ],
      plan,
    });
    expect(label).toBe("正在整理上下文…");
  });

  it("计划当前步作为阶段行（in_progress）", () => {
    expect(runStageLabel({ runId: "run-1", running: true, plan })).toBe(
      "推进「步骤二：起草」…",
    );
  });
});
