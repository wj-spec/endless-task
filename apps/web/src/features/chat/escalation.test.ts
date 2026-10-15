import { describe, expect, it } from "vitest";
import type {
  RuntimeV2Escalation,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "./apiTypes";
import {
  ESCALATION_RESUME_NOTE,
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

  it("其他运行的终态事件不清空当前报告（T4 item 2 误清方向）", () => {
    // 同会话里并发的侧车道/临时 run 结束时，事件窗口（最近 200 条、跨泳道混装）里
    // 会混进**别的** run 的 run.finished/failed/cancelled：正在等用户决策的横幅
    // 必须活下来，否则用户还没看到它就没了。
    for (const type of ["run.finished", "run.failed", "run.cancelled"]) {
      const state = deriveEscalationState(snapshotWith(null), [
        event("run.awaiting_user", payload, "run_1"),
        event(type, { runId: "run_2" }, "run_2"),
      ]);
      expect(state?.runId).toBe("run_1");
    }
  });

  it("别的 run 的 run.awaiting_user 不**替换**当前横幅（A2 item 4 设置方向）", () => {
    // 起点：设置方向无条件覆盖 → 侧车道 run 一升级，用户正等着的横幅就被换成别人。
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", payload, "run_1"),
      event(
        "run.awaiting_user",
        { ...payload, runId: "run_2", summary: "侧车道 run 的升级。" },
        "run_2",
      ),
    ]);
    expect(state?.runId).toBe("run_1");
    expect(state?.summary).toBe("连续 4 轮没有实质进展。");
  });

  it("同一个 run 的 run.awaiting_user 仍然更新当前横幅（不许一刀切成永不更新）", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", payload, "run_1"),
      event(
        "run.awaiting_user",
        {
          ...payload,
          reason: "budget_exhausted",
          summary: "同一个 run 的更新摘要。",
        },
        "run_1",
      ),
    ]);
    expect(state?.runId).toBe("run_1");
    expect(state?.reason).toBe("budget_exhausted");
    expect(state?.summary).toBe("同一个 run 的更新摘要。");
  });

  it("快照说有在飞 run 时，别的 run 的升级事件不立起横幅（runningRunId 同口径）", () => {
    const snapshot = {
      escalation: null,
      runningRunId: "run_main",
    } as unknown as RuntimeV2Snapshot;
    const state = deriveEscalationState(snapshot, [
      event("run.awaiting_user", { ...payload, runId: "run_side" }, "run_side"),
    ]);
    expect(state).toBeNull();
  });

  it("锚点为在飞 run 时，**它自己**的升级事件照常立起横幅（实时升级没被堵死）", () => {
    const snapshot = {
      escalation: null,
      runningRunId: "run_main",
    } as unknown as RuntimeV2Snapshot;
    const state = deriveEscalationState(snapshot, [
      event("run.awaiting_user", { ...payload, runId: "run_main" }, "run_main"),
    ]);
    expect(state?.runId).toBe("run_main");
  });

  it("本 run 的终态事件清空**软阈值**报告（willStop=false 仍如实清）", () => {
    for (const type of ["run.finished", "run.failed", "run.cancelled"]) {
      const state = deriveEscalationState(snapshotWith(null), [
        event("run.awaiting_user", payload, "run_1"),
        event(type, { runId: "run_1" }, "run_1"),
      ]);
      expect(state).toBeNull();
    }
  });

  it("硬停（willStop=true）不被**它自己那个 run** 的终态事件清掉（终审 W1 item 1）", () => {
    // 硬停的收尾顺序是 `run.awaiting_user(willStop=true)` **紧跟** 同一个 run 的
    // `run.finished`；后端 `_escalation_json` 特意为终态的 `willStop=true` 保留投影
    // （"老实停止必须可见"），前端在这里清掉 = 实时会话里横幅归 null（SSE 不重放
    // 历史，只有刷新才从快照拿到那句话）。
    for (const type of ["run.finished", "run.failed", "run.cancelled"]) {
      const state = deriveEscalationState(snapshotWith(null), [
        event("run.awaiting_user", { ...payload, willStop: true }, "run_1"),
        event(type, { runId: "run_1" }, "run_1"),
      ]);
      expect(state?.runId).toBe("run_1");
      expect(state?.willStop).toBe(true);
    }
  });

  it("硬停被**另一个** run 的 run.started 清掉（终审 W1 item 2）", () => {
    // 与后端 `_awaiting_user_scope` 同口径：出现别的 run 的 `run.started`
    // → 这条终端升级的作用域过期，快照不再返回它。
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", { ...payload, willStop: true }, "run_1"),
      event("run.started", { runId: "run_2" }, "run_2"),
    ]);
    expect(state).toBeNull();
  });

  it("硬停不被**同一个 run** 的 run.started 清掉（只认别的 run）", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", { ...payload, willStop: true }, "run_1"),
      event("run.started", { runId: "run_1" }, "run_1"),
    ]);
    expect(state?.runId).toBe("run_1");
  });

  it("硬停仍被**同一个 run** 的进度恢复清掉（不清空规则只对终态事件让路）", () => {
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", { ...payload, willStop: true }, "run_1"),
      event("run.progress_resumed", { runId: "run_1" }, "run_1"),
    ]);
    expect(state).toBeNull();
  });

  it("软阈值（willStop=false）不被别的 run 的 run.started 清掉（口径不扩大）", () => {
    // 规则只覆盖硬停：软阈值在 run 仍在飞时是活的，清空规则保持原样（终态才清）。
    const state = deriveEscalationState(snapshotWith(null), [
      event("run.awaiting_user", payload, "run_1"),
      event("run.started", { runId: "run_2" }, "run_2"),
    ]);
    expect(state?.runId).toBe("run_1");
    expect(state?.willStop).toBe(false);
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

  it("E2 的不确定副作用有自己的标签，不落到兜底文案（批次 E）", () => {
    // 后端 `_REASONS` 新增了 `uncertain_side_effects`；未知 reason 会兜底成
    // 「连续无进展」，那会把"结果无法确认"说成另一件事 ⇒ 必须显式映射。
    expect(escalationReasonLabel("uncertain_side_effects")).toBe(
      "工具结果无法确认",
    );
    expect(escalationReasonLabel("uncertain_side_effects")).not.toBe(
      "连续无进展",
    );
    expect(
      escalationHeadline({
        ...state,
        reason: "uncertain_side_effects",
        willStop: true,
      }),
    ).toContain("工具结果无法确认");
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

  it("硬停（已终态）的 run 上「继续」=「新开一轮」，其余两条出路口径不变（A2 item 7）", () => {
    // 硬停的 run 已经结束（`run.awaiting_user(willStop=true)` 紧跟 `run.finished`），
    // steer 必被后端拒绝；文案必须如实指向"新开一轮"而不是给它发死指令。
    expect(escalationOptionLabel("continue", true)).toBe("新开一轮");
    expect(escalationOptionLabel("continue", false)).toBe("继续");
    expect(escalationOptionLabel("change_approach", true)).toBe("换一条路径");
    expect(escalationOptionLabel("take_over", true)).toBe("人工接管");
  });

  it("E3/B：硬停**且可复活**时「继续」指向同 run 复活（而不是新开一轮）", () => {
    expect(escalationOptionLabel("continue", true, true)).toBe(
      "继续（接上这条运行）",
    );
    // 第三参缺省 = 不可复活 ⇒ 与上一版逐字相同（既有调用方零变化）。
    expect(escalationOptionLabel("continue", true, false)).toBe("新开一轮");
    expect(escalationOptionLabel("continue", true)).toBe("新开一轮");
    // 另外两条出路与"能不能复活"无关。
    expect(escalationOptionLabel("change_approach", true, true)).toBe(
      "换一条路径",
    );
    expect(escalationOptionLabel("take_over", true, true)).toBe("人工接管");
    // 运行还在飞（willStop=false）时第三参不生效：照旧是 steer 的「继续」。
    expect(escalationOptionLabel("continue", false, true)).toBe("继续");
  });

  it("E3/B：复活后的如实说明说清「没有新开一轮」与「停机不计入已用」", () => {
    expect(ESCALATION_RESUME_NOTE).toContain("没有新开一轮");
    expect(ESCALATION_RESUME_NOTE).toContain("停机时间不计入已用时长");
  });

  it("继续指令在有失败记忆时要求不重复失败调用", () => {
    expect(escalationContinueMessage(state)).toContain("不要重复已失败的调用");
    expect(
      escalationContinueMessage({ ...state, repeatedFailures: [] }),
    ).toBe("请继续按当前思路推进。");
  });
});

describe("时长升级文案", () => {
  it("识别时长升级原因并给出中文标签", () => {
    expect(escalationReasonLabel("time_budget_exhausted")).toBe("运行时长已到阈值");
  });

  it("解析时长字段并渲染已用时", () => {
    const state = deriveEscalationState(null, [
      event("run.awaiting_user", {
        reason: "time_budget_exhausted",
        summary: "本次运行已进行 10.5 分钟…",
        options: ["continue", "change_approach", "take_over"],
        progress: { modelTurns: 12, toolCalls: 30, toolFailures: 0, producedCharacters: 2048, elapsedSeconds: 630 },
        budget: { usedTokens: 1000, limitTokens: 131072, usedSeconds: 630, limitSeconds: 3600 },
        willStop: false,
      }),
    ]);
    expect(state?.reason).toBe("time_budget_exhausted");
    expect(state?.progress.elapsedSeconds).toBe(630);
    expect(state?.budget.limitSeconds).toBe(3600);
    expect(escalationProgressText(state!)).toContain("已用时 10.5 分钟");
  });

  it("没有时长字段时不显示已用时（老事件兼容）", () => {
    const state = deriveEscalationState(null, [
      event("run.awaiting_user", {
        reason: "no_progress",
        progress: { modelTurns: 3, toolCalls: 4, toolFailures: 3, producedCharacters: 120 },
        willStop: false,
      }),
    ]);
    expect(escalationProgressText(state!)).not.toContain("已用时");
  });
});
