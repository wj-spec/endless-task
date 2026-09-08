import { describe, expect, it } from "vitest";
import type {
  RuntimeV2Escalation,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";
import {
  deriveEscalationState,
  escalationContinueMessage,
  escalationHeadline,
  escalationOptionLabel,
  escalationProgressText,
  escalationReasonLabel,
} from "./escalation";

const event = (
  type: string,
  data: Record<string, unknown> = {},
  runId = "run_1",
): RuntimeV2ProductEvent =>
  ({
    eventId: `evt_${type}`,
    eventSeq: 1,
    type,
    conversationId: "conv_1",
    laneId: "lane_1",
    runId,
    createdAt: "2026-01-01T00:00:00Z",
    data: { runId, ...data },
  }) as RuntimeV2ProductEvent;

const payload = {
  runId: "run_1",
  reason: "no_progress",
  summary: "连续 4 轮没有实质进展。",
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
  failures: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      attempt: 3,
      toolExecutionId: "exec_3",
    },
  ],
  guidance: "失败记忆：read_file 已连续失败 3 次。",
  willStop: false,
};

const snapshotWith = (escalation: RuntimeV2Escalation | null) =>
  ({ escalation } as unknown as RuntimeV2Snapshot);

describe("deriveEscalationState（C4 终止与升级）", () => {
  it("无快照无事件时为 null", () => {
    expect(deriveEscalationState(null, [])).toBeNull();
    expect(deriveEscalationState(snapshotWith(null), undefined)).toBeNull();
  });

  it("从快照恢复升级报告（刷新后仍可见）", () => {
    const state = deriveEscalationState(
      snapshotWith({
        runId: "run_1",
        reason: "budget_exhausted",
        summary: "预算将尽",
        options: [],
        progress: {},
        budget: {},
        repeatedFailures: [],
        failures: [],
        guidance: "",
        willStop: false,
      }),
      [],
    );
    expect(state?.reason).toBe("budget_exhausted");
  });

  it("实时 run.awaiting_user 事件覆盖快照并解析字段", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", payload),
    ]);
    expect(state?.reason).toBe("no_progress");
    expect(state?.summary).toContain("没有实质进展");
    expect(state?.options).toEqual(["continue", "change_approach", "take_over"]);
    expect(state?.progress.toolFailures).toBe(3);
    expect(state?.budget.usedRatio).toBe(1);
    expect(state?.repeatedFailures[0].toolName).toBe("read_file");
    expect(state?.failures[0].toolExecutionId).toBe("exec_3");
  });

  it("进度恢复或运行结束清空升级报告", () => {
    for (const type of [
      "run.progress_resumed",
      "run.finished",
      "run.failed",
      "run.cancelled",
    ]) {
      const state = deriveEscalationState(snapshotWith(null), [
        event("run.awaiting_user", payload),
        event(type),
      ]);
      expect(state).toBeNull();
    }
  });

  it("其他运行的恢复事件不清空当前报告", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", payload, "run_1"),
      event("run.progress_resumed", { runId: "run_2" }, "run_2"),
    ]);
    expect(state?.runId).toBe("run_1");
  });

  it("缺字段时按默认值容错", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", {}, "run_9"),
    ]);
    expect(state?.reason).toBe("no_progress");
    expect(state?.progress.toolCalls).toBe(0);
    expect(state?.willStop).toBe(false);
  });
});

describe("升级文案", () => {
  const state = deriveEscalationState(snapshotWith(null), [
    event("run.awaiting_user", payload),
  ]) as RuntimeV2Escalation;

  it("标题随原因与是否已停止变化", () => {
    expect(escalationHeadline(state)).toContain("连续无进展");
    expect(escalationHeadline({ ...state, willStop: true })).toContain(
      "已安全停止",
    );
    expect(
      escalationHeadline({ ...state, reason: "budget_exhausted" }),
    ).toContain("上下文预算将尽");
  });

  it("原因标签可读", () => {
    expect(escalationReasonLabel("budget_exhausted")).toBe("上下文预算将尽");
    expect(escalationReasonLabel("no_progress")).toBe("连续无进展");
  });

  it("进展摘要包含轮数/工具/失败/文本", () => {
    expect(escalationProgressText(state)).toBe(
      "已进行 4 轮、工具调用 5 次（失败 3 次）、新增文本 42 字",
    );
  });

  it("选项文案映射到三条出路", () => {
    expect(escalationOptionLabel("continue")).toBe("继续");
    expect(escalationOptionLabel("change_approach")).toBe("换一条路径");
    expect(escalationOptionLabel("take_over")).toBe("人工接管");
  });

  it("继续指令在有失败记忆时要求不重复失败调用", () => {
    expect(escalationContinueMessage(state)).toContain("不要重复已失败的调用");
    expect(
      escalationContinueMessage({ ...state, repeatedFailures: [] }),
    ).toBe("请继续按当前思路推进。");
  });
});
