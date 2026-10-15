import { describe, expect, it } from "vitest";
import type { RuntimeV2RunState } from "./apiTypes";
import {
  connectionPhaseLabel,
  formatElapsedCompact,
  isRunActive,
  runBudgetText,
  runElapsedSeconds,
  runStatusLabel,
  runStepText,
} from "./runHeader";

/**
 * 切片 10 T4（item 1）头部条的纯推导。
 *
 * 这里钉住的核心是一条纪律：**后端没给的字段不许编**。所以每个"缺字段"分支都有一个
 * 反例用例（不给起点就不用"当前时间"、没有预算墙就不显示预算、没有模型轮就"尚未开始"）。
 */
const runState = (
  overrides: Partial<RuntimeV2RunState> = {},
): RuntimeV2RunState => ({
  runId: "run_1",
  status: "running",
  partialContent: "",
  errorCode: null,
  safeMessage: null,
  modelTurns: [],
  ...overrides,
});

const T0 = Date.parse("2026-09-14T10:00:00Z");

describe("活时钟的来源", () => {
  it("终态之外的状态都算在跑（含未知状态，宁可多走一秒也不谎报已停止）", () => {
    for (const status of [
      "created",
      "queued",
      "running",
      "waiting_approval",
      "compacting",
      "cancelling",
      "something_new",
    ]) {
      expect(isRunActive(status)).toBe(true);
    }
    for (const status of ["completed", "failed", "cancelled"]) {
      expect(isRunActive(status)).toBe(false);
    }
  });

  it("运行中：从后端 startedAt 起算，随 now 前进", () => {
    const state = runState({
      status: "running",
      startedAt: "2026-09-14T09:59:00Z",
      elapsedSeconds: 60,
    });
    expect(runElapsedSeconds(state, T0)).toBe(60);
    expect(runElapsedSeconds(state, T0 + 5_000)).toBe(65);
  });

  it("终态：时钟停在后端冻结的 elapsedSeconds，不再随 now 前进", () => {
    const state = runState({
      status: "failed",
      startedAt: "2026-09-14T09:00:00Z",
      elapsedSeconds: 42,
    });
    expect(runElapsedSeconds(state, T0)).toBe(42);
    expect(runElapsedSeconds(state, T0 + 600_000)).toBe(42);
  });

  it("运行中但没有 startedAt：只显示后端快照值，不拿 now 当起点", () => {
    const state = runState({ status: "created", startedAt: null, elapsedSeconds: 7 });
    // "created 但后端没写 started_at"：用 now - 推测起点就是伪造（那会算出 0 秒）。
    expect(runElapsedSeconds(state, T0 + 999_000)).toBe(7);
  });

  it("既没有起点也没有 elapsedSeconds：返回 null，界面显示—", () => {
    expect(
      runElapsedSeconds(runState({ startedAt: null, elapsedSeconds: null }), T0),
    ).toBeNull();
    expect(runElapsedSeconds(runState({}), T0)).toBeNull();
  });

  it("起点比本地时钟靠后（时钟偏差）时钳到 0，不出现负数", () => {
    const state = runState({ startedAt: "2026-09-14T10:00:30Z" });
    expect(runElapsedSeconds(state, T0)).toBe(0);
  });
});

describe("秒数文案", () => {
  it("按秒/分/小时换算（59 秒 / 1 分 / 1 分 5 秒 / 1 小时 / 1 小时 1 分）", () => {
    expect(formatElapsedCompact(59)).toBe("59 秒");
    expect(formatElapsedCompact(60)).toBe("1 分");
    expect(formatElapsedCompact(65)).toBe("1 分 5 秒");
    expect(formatElapsedCompact(3600)).toBe("1 小时");
    expect(formatElapsedCompact(3660)).toBe("1 小时 1 分");
  });

  it("null / NaN / 负数都不编数字", () => {
    expect(formatElapsedCompact(null)).toBe("—");
    expect(formatElapsedCompact(Number.NaN)).toBe("—");
    expect(formatElapsedCompact(-5)).toBe("0 秒");
  });
});

describe("当前步骤", () => {
  it("有在飞工具：轮数 + 正在执行", () => {
    expect(
      runStepText(
        runState({
          currentStep: {
            modelTurnIndex: 2,
            modelTurnCount: 3,
            toolName: "run_shell",
            toolStatus: "running",
            toolTurnIndex: 2,
            toolInFlight: true,
          },
        }),
      ),
    ).toBe("第 3 轮 · 正在执行 run_shell");
  });

  it("没有在飞工具：标明是最后跑过的那个（不冒充当前轮）", () => {
    expect(
      runStepText(
        runState({
          currentStep: {
            modelTurnIndex: 1,
            modelTurnCount: 2,
            toolName: "read_file",
            toolStatus: "completed",
            toolTurnIndex: 0,
            toolInFlight: false,
          },
        }),
      ),
    ).toBe("第 2 轮 · 最后一步 read_file");
  });

  it("一个模型轮都还没有（后端 currentStep 为 null）→ 不给步骤，界面说尚未开始", () => {
    expect(runStepText(runState({ currentStep: null }))).toBeNull();
    expect(runStepText(runState({}))).toBeNull();
  });

  it("只给轮数或只给工具时各说各的", () => {
    expect(
      runStepText(
        runState({
          currentStep: {
            modelTurnIndex: 0,
            modelTurnCount: 1,
            toolName: null,
            toolStatus: null,
            toolTurnIndex: null,
            toolInFlight: false,
          },
        }),
      ),
    ).toBe("第 1 轮");
    expect(
      runStepText(
        runState({
          currentStep: {
            modelTurnIndex: null,
            modelTurnCount: null,
            toolName: "grep",
            toolStatus: "running",
            toolTurnIndex: null,
            toolInFlight: true,
          },
        }),
      ),
    ).toBe("正在执行 grep");
  });
});

describe("预算文案", () => {
  it("没有 run.budget_declared（budget 为 null）→ 不显示预算，不退回默认档", () => {
    expect(runBudgetText(null)).toBeNull();
  });

  it("有硬墙与剩余：同时给剩余与上限", () => {
    expect(
      runBudgetText({ hardSeconds: 3600, remainingSeconds: 3540, usedSeconds: 60 }),
    ).toBe("预算剩余 59 分（上限 1 小时）");
  });

  it("只有硬墙、没有剩余：只说上限", () => {
    expect(runBudgetText({ hardSeconds: 600 })).toBe("预算上限 10 分");
  });

  it("单轮上限与成本上限各自成句", () => {
    expect(runBudgetText({ perTurnSeconds: 120, costCapUsd: 5 })).toBe(
      "单轮 2 分 · 成本上限 $5",
    );
  });

  it("声称声明了墙但字段全空：不显示空壳预算", () => {
    expect(runBudgetText({ source: "run_budget_declared" })).toBeNull();
  });
});

describe("状态与连接相位文案", () => {
  it("run 状态有中文标签，未知状态原样透出（不假装认识）", () => {
    expect(runStatusLabel("running")).toBe("运行中");
    expect(runStatusLabel("cancelled")).toBe("已停止");
    expect(runStatusLabel("brand_new")).toBe("brand_new");
  });

  it("连接相位四档 + 未知", () => {
    expect(connectionPhaseLabel("connected")).toBe("已连接");
    expect(connectionPhaseLabel("reconnecting")).toBe("重连中");
    expect(connectionPhaseLabel("connecting")).toBe("连接中");
    expect(connectionPhaseLabel("idle")).toBe("已停止");
    expect(connectionPhaseLabel(undefined)).toBe("未知");
  });
});
