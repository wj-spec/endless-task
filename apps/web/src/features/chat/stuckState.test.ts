import { describe, expect, it } from "vitest";
import type {
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeV2StuckState,
} from "./apiTypes";
import {
  deriveStuckState,
  repeatedFailureText,
  stuckHeadline,
  stuckSteerMessage,
} from "./stuckState";

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

const stuckPayload = {
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
      safeMessage: "工具暂时不可用。",
      attempt: 2,
      retryable: true,
      toolExecutionId: "exec_2",
    },
  ],
  guidance: "失败记忆：`read_file` 已连续失败 2 次。请更换方法或参数，不要原样重复同一调用。",
};

const snapshotWithStuck = (stuck: RuntimeV2StuckState | null) =>
  ({ stuck } as unknown as RuntimeV2Snapshot);

describe("deriveStuckState（C2 失败记忆）", () => {
  it("没有快照也没有事件时不显示卡住", () => {
    expect(deriveStuckState(null, [])).toBeNull();
    expect(deriveStuckState(snapshotWithStuck(null), undefined)).toBeNull();
  });

  it("刷新页面后从快照恢复卡住态", () => {
    const state = deriveStuckState(
      snapshotWithStuck({
        runId: "run_1",
        level: "restrict",
        detector: "repeated_failure",
        reasons: ["连续失败"],
        repeatedFailures: [],
        attempts: [],
        guidance: "",
      }),
      [],
    );
    expect(state?.level).toBe("restrict");
  });

  it("实时 run.stuck 事件覆盖快照", () => {
    const state = deriveStuckState(snapshotWithStuck(null), [
      event("run.stuck", stuckPayload),
    ]);
    expect(state?.detector).toBe("repeated_failure");
    expect(state?.repeatedFailures[0].toolName).toBe("read_file");
    expect(state?.repeatedFailures[0].count).toBe(2);
    expect(state?.attempts[0].toolExecutionId).toBe("exec_2");
  });

  it("run.progress_resumed 清空卡住态", () => {
    const state = deriveStuckState(snapshotWithStuck(null), [
      event("run.stuck", stuckPayload),
      event("run.progress_resumed"),
    ]);
    expect(state).toBeNull();
  });

  it("运行结束事件也清空卡住态", () => {
    for (const type of ["run.finished", "run.failed", "run.cancelled"]) {
      const state = deriveStuckState(snapshotWithStuck(null), [
        event("run.stuck", stuckPayload),
        event(type),
      ]);
      expect(state).toBeNull();
    }
  });

  it("其他运行的恢复事件不清空当前卡住态", () => {
    const state = deriveStuckState(snapshotWithStuck(null), [
      event("run.stuck", stuckPayload, "run_1"),
      event("run.progress_resumed", { runId: "run_2" }, "run_2"),
    ]);
    expect(state?.runId).toBe("run_1");
  });

  it("缺字段的事件按默认值容错", () => {
    const state = deriveStuckState(snapshotWithStuck(null), [
      event("run.stuck", {}, "run_9"),
    ]);
    expect(state?.level).toBe("remind");
    expect(state?.repeatedFailures).toEqual([]);
    expect(state?.guidance).toBe("");
  });
});

describe("卡住文案", () => {
  const base: RuntimeV2StuckState = {
    runId: "run_1",
    level: "remind",
    detector: "repeated_failure",
    reasons: [],
    repeatedFailures: [
      {
        toolName: "read_file",
        errorCode: "temporary_unavailable",
        count: 2,
      },
    ],
    attempts: [],
    guidance: "",
  };

  it("级别越高措辞越强", () => {
    expect(stuckHeadline(base)).toContain("可能卡住了");
    expect(stuckHeadline({ ...base, level: "restrict" })).toContain("换一条路径");
    expect(stuckHeadline({ ...base, level: "stop" })).toContain("已安全停止");
  });

  it("失败摘要包含工具、错误码与次数", () => {
    expect(repeatedFailureText(base.repeatedFailures[0])).toBe(
      "read_file · temporary_unavailable · 连续 2 次",
    );
  });

  it("换路径指令点名已失败的调用并要求不要重复", () => {
    const message = stuckSteerMessage(base);
    expect(message).toContain("read_file(temporary_unavailable×2)");
    expect(message).toContain("不要原样重复");
  });
});
