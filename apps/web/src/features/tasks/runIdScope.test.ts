import { describe, expect, it } from "vitest";
import type { RuntimeV2ProductEvent, RuntimeV2Snapshot } from "../chat/apiTypes";
import { deriveEscalationState } from "../chat/escalation";
import { deriveStuckState } from "../chat/stuckState";

/**
 * 切片 10 T4（item 5）：`features/tasks/` 的单测里覆盖 item 2 的 runId 两向。
 *
 * 实现在 `features/chat/escalation.ts` / `features/chat/stuckState.ts`（那里也各有
 * 两向用例），这里**再钉一遍任务域的形状**：定时任务/提醒 run 与交互 run 共用同一条
 * 每会话事件流（窗口 200 条、跨泳道混装），所以"到点任务跑了"这件事最容易误清掉
 * 用户在等的那条"需要你决策 / 卡住了"横幅——这正是 review 点名的 bug-like 行为。
 */
const snapshotWith = (state: Record<string, unknown>) =>
  state as unknown as RuntimeV2Snapshot;

const event = (
  type: string,
  runId: string,
  data: Record<string, unknown> = {},
): RuntimeV2ProductEvent =>
  ({
    eventId: `evt_${type}_${runId}`,
    eventSeq: 1,
    type,
    conversationId: "conv_1",
    laneId: "lane_main",
    runId,
    createdAt: "2026-09-14T09:00:00Z",
    data: { runId, ...data },
  }) as RuntimeV2ProductEvent;

const escalationPayload = {
  reason: "no_progress",
  summary: "连续 4 轮没有实质进展。",
  options: ["continue", "change_approach", "take_over"],
  progress: { modelTurns: 4, toolCalls: 5, toolFailures: 3, producedCharacters: 42 },
  willStop: false,
};

const stuckPayload = {
  level: "restrict",
  detector: "repeated_failure",
  reasons: ["`read_file` 已连续失败 3 次"],
  repeatedFailures: [{ toolName: "read_file", errorCode: "tool_error", count: 3 }],
  attempts: [],
  guidance: "换一种方法。",
};

const TERMINAL_TYPES = ["run.finished", "run.failed", "run.cancelled"];

describe("终态事件的 runId 作用域（任务/提醒 run 与交互 run 共处一个事件窗口）", () => {
  it("别的 run（例如到点触发后失败的任务 run）结束，不清掉正在等用户决策的横幅", () => {
    for (const type of TERMINAL_TYPES) {
      const state = deriveEscalationState(snapshotWith({ escalation: null }), [
        event("run.awaiting_user", "run_interactive", escalationPayload),
        event(type, "taskrun_1", { errorCode: "provider_unavailable" }),
      ]);
      expect(state?.runId).toBe("run_interactive");
    }
  });

  it("同一个 run 的终态事件仍然清掉横幅（该清的必须清）", () => {
    for (const type of TERMINAL_TYPES) {
      const state = deriveEscalationState(snapshotWith({ escalation: null }), [
        event("run.awaiting_user", "run_interactive", escalationPayload),
        event(type, "run_interactive"),
      ]);
      expect(state).toBeNull();
    }
  });

  it("别的 run 结束不清掉当前 run 的卡住横幅", () => {
    for (const type of TERMINAL_TYPES) {
      const state = deriveStuckState(snapshotWith({ stuck: null }), [
        event("run.stuck", "run_interactive", stuckPayload),
        event(type, "reminder_run_1"),
      ]);
      expect(state?.runId).toBe("run_interactive");
    }
  });

  it("同一个 run 的终态事件仍然清掉卡住横幅（该清的必须清）", () => {
    for (const type of TERMINAL_TYPES) {
      const state = deriveStuckState(snapshotWith({ stuck: null }), [
        event("run.stuck", "run_interactive", stuckPayload),
        event(type, "run_interactive"),
      ]);
      expect(state).toBeNull();
    }
  });

  it("提醒 run 的终态事件不会替交互 run 清横幅（提醒也走同一条流）", () => {
    const state = deriveStuckState(snapshotWith({ stuck: null }), [
      event("run.stuck", "run_interactive", stuckPayload),
      event("run.progress_resumed", "reminder_run_1"),
      event("run.finished", "reminder_run_1"),
    ]);
    expect(state?.runId).toBe("run_interactive");
  });
});
